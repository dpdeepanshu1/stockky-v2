"""orders/soft_gates.py - E273 round 9 (+ round 10: the pre-open gap block): the "chase / room" gates stop refusing and become SIZE PENALTIES (position-stocks, long entries). Round 31: low relative volume; round 32: the daily-structure gate's chase / stretch / room reasons.

Near the day high (entry.py _range_gate_reject), spike chase (entry_timing), VWAP distance (vwap_gate) and gain room (gain_room) each used to end the entry on their own.
The scorecard of 31 Oct showed single soft factors blocking winners, and the plan says a soft factor is a penalty in the stock's OWN units, not a veto:
  penalty = how many times over its own limit the value is (value / limit, so 1.0 = just past the limit, 2.0 = twice as far), capped per gate at SOFT_GATE_MAX_PENALTY (3.0).
The penalties are collected in one dict with BELOW_OPEN's (E273 round 3), stamped on the trade (flow_json["setup"]["penalties"]) and shrink the size through setup_engine
(size / (1 + total), never under SIZE_FLOOR). Their TOTAL at or above setup_engine.PENALTY_SKIP (4.0) is the only refusal left: two bad factors together skip, one cannot (each is capped
under 4). SOFT_GATES_MODE=veto restores the old one-gate-one-refusal behaviour for all four. Each gate keeps its own switch (SPIKE_GUARD_MODE, GAIN_ROOM_MODE, ENTRY_VWAP_GATE_ENABLED,
MAX_ENTRY_RANGE_POSITION): off / log / 0 means no penalty either. Pure helpers; never raise.
"""
from __future__ import annotations

from typing import Dict, Optional

import config


def mode() -> str:
    """penalty (default) | veto. Anything else reads as penalty."""
    m = str(getattr(config, "SOFT_GATES_MODE", "penalty") or "penalty").strip().lower()
    return m if m in ("penalty", "veto") else "penalty"


def penalty_mode() -> bool:
    return mode() == "penalty"


def cap() -> float:
    try:
        return max(0.1, float(getattr(config, "SOFT_GATE_MAX_PENALTY", 3.0)))
    except (TypeError, ValueError):
        return 3.0


def ratio_penalty(value: Optional[float], limit: Optional[float]) -> Optional[float]:
    """value / limit when value has passed limit (>= 1.0), capped; None when it has not or an input is unusable."""
    try:
        v, l = float(value), float(limit)
        if l <= 0 or v < l:
            return None
        return round(min(cap(), v / l), 4)
    except (TypeError, ValueError):
        return None


def range_penalty(range_pos: Optional[float], floor: Optional[float]) -> Optional[float]:
    """Near-day-high penalty: None below the floor, 1.0 AT the floor, rising linearly to the cap at the day high (range position 1.0)."""
    try:
        rp, fl = float(range_pos), float(floor)
        if rp < fl:
            return None
        top = cap()
        frac = (rp - fl) / max(1.0 - fl, 0.01)
        return round(min(top, 1.0 + (top - 1.0) * max(0.0, frac)), 4)
    except (TypeError, ValueError):
        return None


def rvol_penalty(ratio: Optional[float], floor: Optional[float], hard_frac: Optional[float] = 0.5) -> Optional[float]:
    """E273 round 31: low relative volume. None at or above the floor or when unreadable; 1.0 just under it, rising linearly to the cap at `hard_frac` x the floor;
    None is also returned under that depth, so the caller keeps the old LIQUIDITY_RVOL refusal there."""
    try:
        r, f, h = float(ratio), float(floor), max(0.05, min(0.95, float(hard_frac)))
        if f <= 0 or r >= f or r < f * h:
            return None
        top = cap()
        return round(min(top, 1.0 + (top - 1.0) * (f - r) / (f - f * h)), 4)
    except (TypeError, ValueError):
        return None


_STRUCT_SOFT = ("chase ", "stretched ", "only ")


def _lin(frac: float) -> float:
    """1.0 just past the limit, rising linearly to the cap at the hard ceiling (frac 1.0)."""
    f = 0.0 if frac != frac else max(0.0, min(1.0, frac))
    return round(1.0 + (cap() - 1.0) * f, 4)


def structure_penalties(ver) -> tuple:
    """E273 round 32 (port of Real Trade round 26): the daily-structure gate's SOFT reasons (chase above the demand zone, stretch over the EMA20, room to the next resistance)
    become size penalties (1.0 just past the limit, rising to SOFT_GATE_MAX_PENALTY at the ceiling: STRUCTURE_HARD_MULT x the limit for chase / stretch, a quarter of the
    floor for room). Returns (refusal text or None, {penalty name: value}). Any hard reason (bearish daily trend, a fresh bearish break, a thin-volume breakout in enforce),
    a soft reason past its ceiling, a metric that cannot be scored, `veto` mode or any surprise = the gate's own refusal (ver["reason"]). Never raises."""
    try:
        full = ver.get("reason")
        reasons = list(ver.get("reasons") or [])
        if not ver.get("would_reject") or not reasons or not penalty_mode():
            return full, {}
        if any(not str(r).startswith(_STRUCT_SOFT) for r in reasons):
            return full, {}
        m = ver.get("metrics") or {}
        hm = max(1.05, float(getattr(config, "STRUCTURE_HARD_MULT", 1.75) or 1.75))
        pens: dict = {}
        for key, cfgname, name in (("chase_atr", "STRUCTURE_MAX_CHASE_ATR", "structure_chase"), ("stretch_atr", "STRUCTURE_MAX_STRETCH_ATR", "structure_stretch")):
            v, lim = m.get(key), float(getattr(config, cfgname, 0.0) or 0.0)
            if v is not None and lim > 0 and v > lim:
                if v >= lim * hm:
                    return full, {}
                pens[name] = _lin((v - lim) / (lim * hm - lim))
        room, lim = m.get("room_atr"), float(getattr(config, "STRUCTURE_MIN_ROOM_ATR", 0.0) or 0.0)
        if room is not None and lim > 0 and room < lim:
            hard = lim * 0.25
            beyond = float(getattr(config, "STRUCTURE_ROOM_BEYOND_PENALTY", 0.0) or 0.0)
            if room < 0 and beyond > 0:
                pens["structure_room"] = round(min(cap(), beyond), 4)      # E285 round 129: price is already past the cached resistance (a breakout), not short of room
            elif room <= hard:
                return full, {}
            else:
                pens["structure_room"] = _lin((lim - room) / (lim - hard))
        if len(pens) < len(reasons):          # a soft reason we could not score: keep the gate's own refusal
            return full, {}
        return None, pens
    except Exception:  # noqa: BLE001
        return (ver.get("reason") if isinstance(ver, dict) else None), {}


def drift_penalty(slip_pct, limit_pct) -> Optional[float]:
    """E273 round 33 (port of Real Trade round 28): the entry-slippage CHASE check (live price above the signal price by more than ENTRY_MAX_SLIPPAGE_PCT). 1.0 just past
    the limit, rising linearly to the cap at DRIFT_HARD_MULT x the limit; None at or under the limit, at or past that ceiling (the old ENTRY_SLIPPAGE refusal stands), in
    veto mode or when an input is unusable. Never raises."""
    try:
        if not penalty_mode():
            return None
        d, lim = float(slip_pct), float(limit_pct)
        if d != d or lim != lim or lim <= 0 or d <= lim:
            return None
        hard = lim * max(1.05, float(getattr(config, "DRIFT_HARD_MULT", 1.75) or 1.75))
        if d >= hard:
            return None
        return _lin((d - lim) / (hard - lim))
    except (TypeError, ValueError):
        return None


def day_gain_penalty(gain_pct, limit_pct) -> Optional[float]:
    """E273 round 34: the day-gain guard (MAX_DAY_GAIN_PCT). 1.0 just past the limit, rising linearly to the cap at DAY_GAIN_HARD_MULT x the limit; None at or under the
    limit, at or past that ceiling (the old DAY_GAIN_TOO_HIGH refusal stands), in veto mode or when an input is unusable. Never raises."""
    try:
        if not penalty_mode():
            return None
        d, lim = float(gain_pct), float(limit_pct)
        if d != d or lim != lim or lim <= 0 or d <= lim:
            return None
        hard = lim * max(1.05, float(getattr(config, "DAY_GAIN_HARD_MULT", 1.75) or 1.75))
        if d >= hard:
            return None
        return _lin((d - lim) / (hard - lim))
    except (TypeError, ValueError):
        return None


def late_size(setup_mult: Optional[float], early: Optional[Dict[str, float]], late: Optional[Dict[str, float]], floor: float = 0.25) -> Optional[float]:
    """E273 round 32: size multiplier once penalties that arrive AFTER the setup engine ran (relative volume, daily structure) are counted. The engine's size was
    base / (1 + early total); with the late ones it is base / (1 + early + late), never under `floor`. `setup_mult` None (no setup, no early penalty) = a base of 1.0.
    Returns the new multiplier, or `setup_mult` unchanged when there is no late penalty or an input is unusable. Never raises."""
    try:
        lt = sum(float(v) for v in (late or {}).values() if isinstance(v, (int, float)) and not isinstance(v, bool))
        if lt <= 0:
            return setup_mult
        et = sum(float(v) for v in (early or {}).values() if isinstance(v, (int, float)) and not isinstance(v, bool))
        base_x = (1.0 if setup_mult is None else float(setup_mult)) * (1.0 + max(0.0, et))
        return round(max(floor, base_x / (1.0 + max(0.0, et) + lt)), 4)
    except (TypeError, ValueError):
        return setup_mult


def skip_limit() -> float:
    """E285 round 129: the penalty total at or above which an entry is skipped. SOFT_PENALTY_SKIP_AT (6.0), never under the engine's own 4.0. Read at call time. Never raises."""
    try:
        return max(4.0, float(getattr(config, "SOFT_PENALTY_SKIP_AT", 4.0)))
    except (TypeError, ValueError):
        return 4.0


def total_skip_reason(penalties: Optional[Dict[str, float]], skip_at: float) -> Optional[str]:
    """SOFT_PENALTIES_TOO_HIGH reason when the penalties add up to skip_at or more, else None."""
    try:
        items = {str(k): float(v) for k, v in (penalties or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        tot = sum(items.values())
        if tot < skip_at:
            return None
        parts = ", ".join(f"{k} {v:.1f}" for k, v in sorted(items.items(), key=lambda kv: -kv[1]) if v > 0)
        return f"SOFT_PENALTIES_TOO_HIGH:total {tot:.1f} >= {skip_at:g} ({parts})"
    except Exception:  # noqa: BLE001
        return None
