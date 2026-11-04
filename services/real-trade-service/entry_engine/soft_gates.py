"""entry_engine/soft_gates.py - E273 round 24 (+ round 25 pre-open gap): VWAP-below, spike chase, gain room and the unexplained pre-open gap become SIZE PENALTIES in the Real Trade long entry.

The plan's "soft factor = a penalty, not a veto" (Position Stocks got it in round 9). Each gate keeps its own limit; past the limit but short of a hard ceiling the
entry is sized x0.85 falling linearly to x0.40 at the ceiling, and only past the ceiling is it refused with the old reason. Two or more penalties multiply, floor
`SOFT_GATE_FLOOR` (0.25). `SOFT_GATES_MODE` veto restores the old refusals exactly. Pure, never raises; missing data is no opinion (the old gates already fail open).
"""
from __future__ import annotations

from typing import Optional, Tuple

import config
from entry_engine import day_spike_gate, gap_event, liquidity_gate, preopen, vwap_gate

START, END = 0.85, 0.40          # multiplier just past the limit / at the hard ceiling (same ramp as the opening-gate penalty, round 17)


def mode() -> str:
    m = str(getattr(config, "SOFT_GATES_MODE", "penalty") or "penalty").strip().lower()
    return "veto" if m == "veto" else "penalty"


def floor() -> float:
    try:
        return max(0.05, min(1.0, float(getattr(config, "SOFT_GATE_FLOOR", 0.25))))
    except (TypeError, ValueError):
        return 0.25


def ramp(frac: float) -> float:
    """0 = just past the limit -> START; 1 = at the hard ceiling -> END (clamped)."""
    f = 0.0 if frac != frac else max(0.0, min(1.0, frac))
    return round(START + (END - START) * f, 4)


def combine(mults) -> float:
    out = 1.0
    for m in mults:
        out *= m
    return round(max(floor(), out), 4) if out < 1.0 else 1.0


def _cfg(name: str, default: float) -> float:
    try:
        v = float(getattr(config, name, default))
        return v if v == v and v > 0 else default
    except (TypeError, ValueError):
        return default


def vwap(tick) -> Tuple[Optional[str], float]:
    """(refusal reason | None, multiplier). Under the VWAP by more than the tolerance = penalty; by `VWAP_HARD_UNDER_PCT` or more = the old refusal."""
    try:
        why = vwap_gate.reject_reason_tick(tick)
        if not why:
            return None, 1.0
        if mode() == "veto":
            return why, 1.0
        p, v = float(tick.price), float(tick.vwap)
        under = (v - p) / v * 100.0
        tol = vwap_gate.tolerance_for_tick(tick)                      # E285 round 111: the stock's own tolerance when known
        hard = max(tol + 0.01, _cfg("VWAP_HARD_UNDER_PCT", 1.5))
        if under >= hard:
            return why, 1.0
        return None, ramp((under - tol) / (hard - tol))
    except Exception:  # noqa: BLE001
        return None, 1.0


def spike(tick) -> Tuple[Optional[str], float]:
    """Spike chase: day gain over its limit AND high in the range = penalty; at `SPIKE_HARD_MULT` x the limit (or more) = the old refusal."""
    try:
        why = day_spike_gate.spike_reason_tick(tick)
        if not why:
            return None, 1.0
        if mode() == "veto":
            return why, 1.0
        pc, p = float(tick.prev_close), float(tick.price)
        day_pct = (p - pc) / pc * 100.0
        lim = day_spike_gate.day_limit_pct(day_spike_gate._num(day_spike_gate._atr_pct(tick)))
        if lim <= 0:
            return why, 1.0
        hard = lim * max(1.05, _cfg("SPIKE_HARD_MULT", 1.75))
        if day_pct >= hard:
            return why, 1.0
        return None, ramp((day_pct - lim) / (hard - lim))
    except Exception:  # noqa: BLE001
        return None, 1.0


def gain_room(tick, target_pct) -> Tuple[Optional[str], float]:
    """Range used up: room left under what the target needs = penalty; under `GAIN_ROOM_HARD_FRAC` of the need = the old refusal."""
    try:
        why = day_spike_gate.range_reason_tick(tick, target_pct)
        if not why:
            return None, 1.0
        if mode() == "veto":
            return why, 1.0
        p, lo = float(tick.price), float(tick.day_low)
        atr, tp = float(day_spike_gate._atr_pct(tick)), float(target_pct)
        room = (lo * (1.0 + float(getattr(config, "GAIN_ROOM_RANGE_MULT", 2.0)) * atr / 100.0) - p) / p * 100.0
        need = float(getattr(config, "GAIN_ROOM_MIN_TARGET_FRAC", 0.5)) * tp
        hard_room = need * min(0.9, _cfg("GAIN_ROOM_HARD_FRAC", 0.25))
        if room < hard_room or need <= hard_room:
            return why, 1.0
        return None, ramp((need - room) / (need - hard_room))
    except Exception:  # noqa: BLE001
        return None, 1.0


def preopen_gap(symbol: str, reason: Optional[str]) -> Tuple[Optional[str], float]:
    """E273 round 25: an UNEXPLAINED pre-open gap (`reason` = preopen.gap_reason, no fresh positive event behind it) past its limit = penalty; at `PREOPEN_GAP_HARD_MULT` x the limit
    (up 6 % / down 3 % by default, so 12 % / 6 %) or more = the old refusal. A gap explained by an event never reaches here (round 20 waives it, round 21 skips a gap down against it)."""
    try:
        if not reason:
            return None, 1.0
        if mode() == "veto":
            return reason, 1.0
        kind, g = gap_event.gap_kind(reason), preopen.gap_for(symbol)
        if kind not in ("up", "down") or g is None:
            return reason, 1.0
        lim = float(getattr(config, "PREOPEN_GAP_UP_BLOCK_PCT" if kind == "up" else "PREOPEN_GAP_DOWN_BLOCK_PCT", 0.0) or 0.0)
        if lim <= 0:
            return reason, 1.0
        hard = lim * max(1.05, _cfg("PREOPEN_GAP_HARD_MULT", 2.0))
        size = abs(float(g))
        if size >= hard:
            return reason, 1.0
        return None, ramp((size - lim) / (hard - lim))
    except Exception:  # noqa: BLE001
        return reason, 1.0


def liquidity(tick) -> Tuple[Optional[str], float]:
    """E273 round 29: the RELATIVE-VOLUME test of the liquidity gate (volume under `ENTRY_MIN_RVOL` x its own normal pace for the time of day) = penalty; under `LIQUIDITY_RVOL_HARD_FRAC`
    (0.5) of that floor = the old refusal. The average-turnover and day-turnover tests (can the position be sold at the stop?) are hard and never reach the penalty path."""
    try:
        why = liquidity_gate.reject_reason_tick(tick)
        if not why:
            return None, 1.0
        if mode() == "veto" or not str(why).startswith("LIQUIDITY_RVOL"):
            return why, 1.0
        r = liquidity_gate.rvol_ratio(tick)
        floor_ = _cfg("ENTRY_MIN_RVOL", 0.35)
        hard = floor_ * max(0.05, min(0.95, _cfg("LIQUIDITY_RVOL_HARD_FRAC", 0.5)))
        if r is None or r < hard:
            return why, 1.0
        return None, ramp((floor_ - r) / (floor_ - hard))
    except Exception:  # noqa: BLE001
        return None, 1.0


def drift(reason: Optional[str], drift_pct, max_drift_pct) -> Tuple[Optional[str], float]:
    """E273 round 28: the entry-drift CHASE check (price ran above the signal price by more than `ENTRY_MAX_DRIFT_ATR` x ATR). Past the limit = penalty (x0.85 falling to x0.40);
    at `DRIFT_HARD_MULT` x the limit (1.75) or more = the old refusal. Only the up-chase is soft; `reason` empty (check passed) or a fall below the signal is not handled here."""
    try:
        if not reason:
            return None, 1.0
        if mode() == "veto":
            return reason, 1.0
        d, lim = float(drift_pct), float(max_drift_pct)
        if d <= 0 or lim <= 0 or d != d or lim != lim:
            return reason, 1.0
        hard = lim * max(1.05, _cfg("DRIFT_HARD_MULT", 1.75))
        if d >= hard:
            return reason, 1.0
        return None, ramp((d - lim) / (hard - lim))
    except Exception:  # noqa: BLE001
        return reason, 1.0


_SOFT_PREFIX = ("chase ", "price stretched", "only ", "structure R:R")


def structure(ver) -> Tuple[Optional[str], dict]:
    """E273 round 26: the daily-structure gate's SOFT reasons (chase above the demand zone, stretch over the EMA20, room to the next resistance, structure R:R) become size
    penalties; the hard ones (bearish structure or a fresh bearish break, a too-wide structural stop, a breakout-volume failure in enforce) still refuse. Returns
    (refusal text or None, {penalty name: multiplier}). A soft reason past its ceiling (`STRUCTURE_HARD_MULT` x the limit; room under a quarter of its floor; R:R under half
    of its floor) refuses with the gate's own text. `veto` mode, a missing metric or any surprise = the gate's own verdict. Never raises."""
    try:
        full = ver.get("reason")
        reasons = list(ver.get("reasons") or [])
        if not ver.get("would_reject") or not reasons or mode() == "veto":
            return full, {}
        if any(not str(r).startswith(_SOFT_PREFIX) for r in reasons):
            return full, {}
        m = ver.get("metrics") or {}
        hm = max(1.05, _cfg("STRUCTURE_HARD_MULT", 1.75))
        pens: dict = {}
        for key, cfgname, name in (("chase_atr", "STRUCTURE_MAX_CHASE_ATR", "structure_chase"), ("stretch_atr", "STRUCTURE_MAX_STRETCH_ATR", "structure_stretch")):
            v, lim = m.get(key), float(getattr(config, cfgname, 0.0) or 0.0)
            if v is not None and lim > 0 and v > lim:
                if v >= lim * hm:
                    return full, {}
                pens[name] = ramp((v - lim) / (lim * hm - lim))
        room, lim = m.get("room_atr"), float(getattr(config, "STRUCTURE_MIN_ROOM_ATR", 0.0) or 0.0)
        if room is not None and lim > 0 and room < lim:
            hard = lim * 0.25
            beyond = _cfg("STRUCTURE_ROOM_BEYOND_MULT", 0.0)
            if room < 0 and 0 < beyond < 1.0:
                pens["structure_room"] = round(max(END, beyond), 4)     # E286 round 216: the price is already PAST the cached resistance (a breakout), not short of room: a fixed size cut
            elif room <= hard:
                return full, {}
            else:
                pens["structure_room"] = ramp((lim - room) / (lim - hard))
        rr, lim = m.get("rr"), float(getattr(config, "STRUCTURE_MIN_RR", 0.0) or 0.0)
        if rr is not None and lim > 0 and rr < lim:
            hard = lim * 0.5
            if rr <= hard:
                return full, {}
            pens["structure_rr"] = ramp((lim - rr) / (lim - hard))
        if len(pens) < len(reasons):          # a soft reason we could not score: keep the gate's own refusal
            return full, {}
        return None, pens
    except Exception:  # noqa: BLE001
        return ver.get("reason") if isinstance(ver, dict) else None, {}
