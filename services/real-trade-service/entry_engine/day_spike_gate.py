"""entry_engine/day_spike_gate.py - E252 (2026-10-28): the position-stocks E251 day-spike guard and range-used-up check, for real-trade-service.

ARVIND 28 Oct (position-stocks): market buy right after a vertical day move, stopped inside a minute, then the stock ran to target. A real-trade BUY is
sized and priced off the polled tick (no tick buffer here), so only the two day-level reads are ported; the tick-level "thrust" read is not.

  spike_reason(tick):  "SPIKE_CHASE:DAY_SPIKE" when the stock is up >= limit on the day (SPIKE_DAY_ATR_MULT x its own daily ATR %, clamped
                       SPIKE_DAY_MIN_PCT..SPIKE_DAY_MAX_PCT; the fixed SPIKE_DAY_PCT without an ATR) AND in the top of today's range (>= SPIKE_RANGE_POS).
  range_reason(tick, target_pct): "GAIN_ROOM:RANGE" when the room from the price to today's low x (1 + GAIN_ROOM_RANGE_MULT x the daily ATR %) is under
                       GAIN_ROOM_MIN_TARGET_FRAC x the target %. Daily ATR % stands in for the average daily range.

SPIKE_GUARD_MODE / GAIN_ROOM_MODE: live (refuse -> WAIT) | log (log only) | off. Every read fails OPEN on missing data. Never raises.
The stop widening and the reclaim re-entry of E251 are NOT ported (a real-trade stop-out already blocks a same-day re-buy: stopout_guard).
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

import config

logger = logging.getLogger("real-trade-day-spike-gate")

# E257: how often each guard decided something. Without this a cold ATR cache (peek only, no pre-warm) makes both guards silently allow every entry and nobody can tell.
# In-process counters since the last restart; read through GET /entry/guard-stats. outcome: held | log_only | passed | no_data | error.
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Dict[str, int]] = {"spike": {}, "range": {}}


def _bump(gate: str, outcome: str) -> None:
    with _STATS_LOCK:
        d = _STATS[gate]
        d[outcome] = d.get(outcome, 0) + 1


def guard_stats() -> Dict[str, Dict[str, int]]:
    with _STATS_LOCK:
        return {g: dict(v) for g, v in _STATS.items()}


def reset_stats() -> None:
    with _STATS_LOCK:
        for v in _STATS.values():
            v.clear()


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _norm(v) -> str:
    m = str(v or "live").strip().lower()
    return m if m in ("live", "log", "off") else "live"


def _spike_mode() -> str:
    return _norm(getattr(config, "SPIKE_GUARD_MODE", "live") and config.SPIKE_GUARD_MODE)


def _range_mode() -> str:
    return _norm(getattr(config, "GAIN_ROOM_MODE", "live") and config.GAIN_ROOM_MODE)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None


def day_limit_pct(atr_pct: Optional[float]) -> float:
    fixed = float(_cfg("SPIKE_DAY_PCT", 4.0))
    if fixed <= 0:
        return 0.0
    mult = float(_cfg("SPIKE_DAY_ATR_MULT", 2.0))
    if atr_pct and atr_pct > 0 and mult > 0:
        return max(float(_cfg("SPIKE_DAY_MIN_PCT", 2.5)), min(float(_cfg("SPIKE_DAY_MAX_PCT", 6.0)), mult * float(atr_pct)))
    return fixed


def judge_spike(price, prev_close, day_high, day_low, atr_pct) -> Optional[str]:
    """Reason on plain numbers or None. Pure."""
    p, pc, hi, lo = _num(price), _num(prev_close), _num(day_high), _num(day_low)
    if not p or not pc or not hi or not lo or hi <= lo:
        return None
    day_pct = (p - pc) / pc * 100.0
    range_pos = max(0.0, min(1.0, (p - lo) / (hi - lo)))
    lim, pos_lim = day_limit_pct(_num(atr_pct)), float(_cfg("SPIKE_RANGE_POS", 0.85))
    if lim > 0 and day_pct >= lim and range_pos >= pos_lim:
        return (f"SPIKE_CHASE:DAY_SPIKE:up {day_pct:.1f}% on the day (limit {lim:.1f}%) and at range position {range_pos:.2f} "
                f"(limit {pos_lim:.2f}) - wait for a pullback")
    return None


def judge_range(price, day_low, atr_pct, target_pct) -> Optional[str]:
    """Reason on plain numbers or None. Pure."""
    p, lo, atr, tp = _num(price), _num(day_low), _num(atr_pct), _num(target_pct)
    frac = float(_cfg("GAIN_ROOM_MIN_TARGET_FRAC", 0.5))
    if frac <= 0 or not p or not lo or not atr or not tp:
        return None
    mult = float(_cfg("GAIN_ROOM_RANGE_MULT", 2.0))
    expected_high = lo * (1.0 + mult * atr / 100.0)
    room = (expected_high - p) / p * 100.0
    need = frac * tp
    if room < need:
        return (f"GAIN_ROOM:RANGE:only {max(room, 0.0):.2f}% left to {mult:g}x its normal daily range ({atr:.2f}%) from today's low "
                f"{lo:.2f}; the {tp:.2f}% target needs at least {need:.2f}%")
    return None


def _atr_pct(tick) -> Optional[float]:
    try:
        from entry_engine import prev_day
        sym = str(getattr(tick, "symbol", "") or "").upper().replace(".NS", "").strip()
        pdv = prev_day.peek(sym)
        return pdv.atr_pct if pdv else None
    except Exception:  # noqa: BLE001
        return None


def _apply(mode: str, why: Optional[str], tick) -> Optional[str]:
    if why and mode == "log":
        logger.info("day-spike gate (log only): %s would be held back - %s", getattr(tick, "symbol", "?"), why)
        return None
    return why


def spike_reason_tick(tick) -> Optional[str]:
    try:
        m = _spike_mode()
        if m == "off" or tick is None:
            return None
        vals = [_num(getattr(tick, n, None)) for n in ("price", "prev_close", "day_high", "day_low")]
        atr = _atr_pct(tick)
        why = judge_spike(vals[0], vals[1], vals[2], vals[3], atr)
        out = _apply(m, why, tick)
        # no_data = an input of the check was missing (the check then allows); a missing ATR only loosens the limit to the fixed 4%, so it is not counted here
        _bump("spike", "no_data" if (not all(vals) or vals[2] <= vals[3]) else "held" if out else "log_only" if why else "passed")
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("day-spike gate failed (allowing): %s", e)
        _bump("spike", "error")
        return None


def range_reason_tick(tick, target_pct) -> Optional[str]:
    try:
        m = _range_mode()
        if m == "off" or tick is None:
            return None
        price, low, atr = _num(getattr(tick, "price", None)), _num(getattr(tick, "day_low", None)), _atr_pct(tick)
        why = judge_range(price, low, atr, target_pct)
        out = _apply(m, why, tick)
        _bump("range", "no_data" if not (price and low and _num(atr) and _num(target_pct)) else "held" if out else "log_only" if why else "passed")
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("gain-room range gate failed (allowing): %s", e)
        _bump("range", "error")
        return None
