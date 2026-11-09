"""stockky_ms.ms_types - constants and tiny helpers shared by every engine module. Pure stdlib, no I/O, no clock, no env.

A bar is a tuple/list (t, open, high, low, close, volume); t = epoch seconds of the bar start. Enums are plain strings.
"""
from __future__ import annotations

import math
from typing import Any, Optional

MS_ENGINE_VERSION = "1.0.0"
SCHEMA_VERSION = 1

T, O, H, L, C, V = 0, 1, 2, 3, 4, 5

IST_OFFSET_S = 19800

Q_OK, Q_LIMITED, Q_INSUFFICIENT, Q_STALE, Q_UNAVAILABLE = "ok", "limited", "insufficient", "stale", "unavailable"
SIGNAL_QUALITIES = (Q_OK, Q_LIMITED)

BULLISH, BEARISH, NEUTRAL, TRANSITION = "bullish", "bearish", "neutral", "transition"


def finite(v: Any) -> bool:
    try:
        return v is not None and not isinstance(v, bool) and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def r2(v: Optional[float]) -> Optional[float]:
    return round(float(v), 2) if finite(v) else None


def r3(v: Optional[float]) -> Optional[float]:
    return round(float(v), 3) if finite(v) else None


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v
