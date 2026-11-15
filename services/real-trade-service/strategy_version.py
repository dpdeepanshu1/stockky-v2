"""strategy_version.py - round 557 (E582): a short stamp of the strategy settings in force when an entry order is placed.

The stamp is "sv-" + the first 8 hex characters of a SHA-1 over the running values of the calibrated switches below (the real-trade side of the auto-calibration rules in
calibration-runner-service/autocalib.py). Same values -> same stamp; any change of one of them -> a new stamp. It lets the evidence report, the strategy registry and the drift
monitor tell "the strategy changed" apart from "the strategy decayed". None of these switches is a Market Weather overlay dial (those are risk and time limits, stamped separately as
profile_version), so a one-day weather overlay never changes the stamp.

Descriptive only: nothing reads it to gate, size or score a trade. Pure and stdlib only; `current()` never raises (stamping must never break an order): any problem gives None,
which is stored as NULL and read as "no stamp". If you add a calibrated switch to autocalib.py, add it to KEYS too: the stamp then changes once, at that moment.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

KEYS = (
    "KRONOS_GATE_MODE", "TIMESFM_GATE_MODE", "STRUCTURE_STOP_MODE", "STRUCTURE_EXIT_MODE", "STRUCTURE_GATE_MODE",
    "STRUCTURE_MAX_CHASE_ATR", "STRUCTURE_MAX_STRETCH_ATR", "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_MIN_RR", "OVERNIGHT_HOLD_ENABLED",
)
PREFIX = "sv-"
WIDTH = 8


def _norm(v: Any) -> str:
    """One stable text per value: floats lose float noise (1.5 and 1.50 agree), booleans and strings are lower-cased."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(round(v, 6))
    return str(v).strip().lower()


def compute(values: Dict[str, Any]) -> Optional[str]:
    """The stamp for a {switch: value} mapping; only KEYS count, in sorted order. None when none of the KEYS is present."""
    picked = {k: _norm(values[k]) for k in KEYS if k in values and values[k] is not None}
    if not picked:
        return None
    blob = json.dumps(picked, sort_keys=True, separators=(",", ":"))
    return PREFIX + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:WIDTH]


def current() -> Optional[str]:
    """The stamp for the running config module's values right now; None on any problem."""
    try:
        import config
        return compute({k: getattr(config, k) for k in KEYS if hasattr(config, k)})
    except Exception:  # noqa: BLE001 - stamping must never break an order
        return None
