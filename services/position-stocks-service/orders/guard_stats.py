"""orders/guard_stats.py - E257: in-process counters for the entry guards (spike chase, gain room), read through GET /entry/guard-stats.

outcome: held (entry refused) | log_only (would have been refused, mode=log) | passed | no_data (an input was missing, the guard had no opinion) | error.
Without this a guard that never gets its inputs (cold ATR cache, no day range, too few bars) silently allows every entry and nothing shows it. Never raises.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict

logger = logging.getLogger("position-stocks-guard-stats")

_LOCK = threading.Lock()
_STATS: Dict[str, Dict[str, int]] = {}


def bump(gate: str, outcome: str) -> None:
    try:
        with _LOCK:
            d = _STATS.setdefault(gate, {})
            d[outcome] = d.get(outcome, 0) + 1
    except Exception as e:  # noqa: BLE001 - a counter must never break an entry
        logger.debug("guard_stats: bump(%r, %r) failed: %s", gate, outcome, e)


def snapshot() -> Dict[str, Dict[str, int]]:
    with _LOCK:
        return {g: dict(v) for g, v in _STATS.items()}


def reset() -> None:
    with _LOCK:
        _STATS.clear()
