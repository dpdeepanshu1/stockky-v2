"""Group 349: one place that decides whether the Market Board zones/swings come from the legacy code or from the new engine adapter.

BOARD_USE_MS_ENGINE=0 (default) -> legacy `screening.market_structure` (unchanged behaviour).
BOARD_USE_MS_ENGINE=1           -> `stockky_ms.compat_board` (same output shape; one documented one-bar difference in zone status).
Any import or runtime error in the new path falls back to the legacy result for that call, so the board never breaks because of it.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

import config
from screening import market_structure as ms

logger = logging.getLogger("position-stocks.ms_switch")

try:
    from stockky_ms import compat_board as _cb
except Exception:  # noqa: BLE001 - package not vendored in this image
    _cb = None


# group 351: observability. Process-local counters; read by /board/status. Never raise.
_stats = {"engine_calls": 0, "legacy_calls": 0, "fallbacks": 0, "last_error": None}


def stats() -> dict:
    """What the board is actually running on: requested flag, whether the adapter is importable/active, engine version, call counts."""
    ver = None
    try:
        from stockky_ms.ms_types import MS_ENGINE_VERSION as ver  # noqa: N811
    except Exception:  # noqa: BLE001
        ver = None
    return {"requested": bool(getattr(config, "BOARD_USE_MS_ENGINE", False)), "vendored": _cb is not None, "active": engine_active(),
            "engine_version": ver, **_stats}


def reset_stats() -> None:
    _stats.update(engine_calls=0, legacy_calls=0, fallbacks=0, last_error=None)


def engine_active() -> bool:
    return bool(getattr(config, "BOARD_USE_MS_ENGINE", False)) and _cb is not None


def zones_and_swings(bars, atr_mult: float, full_range: bool) -> Tuple[List[dict], List[dict]]:
    if engine_active():
        try:
            out = _cb.find_zones(bars, atr_mult=atr_mult, full_range=full_range), _cb.swings(bars)
            _stats["engine_calls"] += 1
            return out
        except Exception as e:  # noqa: BLE001
            _stats["fallbacks"] += 1
            _stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.warning("ms engine board path failed (%s); using legacy", e)
    _stats["legacy_calls"] += 1
    return ms.find_zones(bars, atr_mult=atr_mult, full_range=full_range), ms.swings(bars)
