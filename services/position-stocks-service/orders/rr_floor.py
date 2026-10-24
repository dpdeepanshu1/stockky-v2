"""orders/rr_floor.py - reward:risk floor on the FINAL stop / target of an automatic entry (2026-10-22, 1:2).

adaptive.compute() builds a 2:1 target, but the later target cap (SCALP_TARGET_CAP_PCT) and the stop floors can leave 1.5:1 or less. This judges the
levels that will actually be placed: target_pct / stop_pct below config.ENTRY_MIN_REWARD_RISK (default 2.0, 0 = off) -> reject reason string, else None.
Pure; never raises (a bad value returns None and the entry is unaffected).
"""
from __future__ import annotations

from typing import Any, Optional

_EPS = 0.005   # levels are rounded to 4 dp percentages; 1.996 is 2.0


def reward_risk(levels: Any) -> Optional[float]:
    try:
        t, s = float(levels.target_pct), float(levels.stop_pct)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (s > 0) or not (t == t) or not (s == s):
        return None
    return t / s


def reject_reason(levels: Any, minimum: Optional[float] = None) -> Optional[str]:
    try:
        if minimum is None:
            import config
            minimum = float(getattr(config, "ENTRY_MIN_REWARD_RISK", 0.0) or 0.0)
        if minimum <= 0:
            return None
        rr = reward_risk(levels)
        if rr is None or rr + _EPS >= minimum:
            return None
        return (f"RR_FLOOR: reward:risk {rr:.2f}:1 (target {float(levels.target_pct):.2f}% / stop {float(levels.stop_pct):.2f}%) "
                f"is below the {minimum:g}:1 floor")
    except Exception:  # noqa: BLE001
        return None


def geometry_reason(levels: Any, entry_ref: float) -> Optional[str]:
    """Short selling step 4: reject levels whose legs sit on the wrong side of the entry price for their direction (long: stop < entry < target;
    short: target < entry < stop). A wrong-way stop would sit on the PROFIT side and never protect the trade. Reason string, else None.
    Missing data (no prices / no ref) returns None; a bad side value returns a reason (never guesses a direction). Never raises."""
    try:
        side = str(getattr(levels, "side", "LONG") or "LONG").strip().upper()
        if side not in ("LONG", "SHORT"):
            return f"LEVEL_GEOMETRY: unknown side {side!r}"
        t, s_, ref = float(levels.target_price), float(levels.stop_price), float(entry_ref)
        if not (ref > 0) or not (t > 0) or not (s_ > 0):
            return None
        if side == "LONG" and not (s_ < ref < t):
            return f"LEVEL_GEOMETRY: long needs stop < entry < target (stop {s_}, entry {ref}, target {t})"
        if side == "SHORT" and not (t < ref < s_):
            return f"LEVEL_GEOMETRY: short needs target < entry < stop (target {t}, entry {ref}, stop {s_})"
        return None
    except Exception:  # noqa: BLE001
        return None
