"""orders/gate_policy.py - E285 batch 5: the adaptive gate policy, READ side (position-stocks longs).

Four entry gates are allowed to move between three states, decided by a nightly job (orders/gate_policy_job.py) from a rolling 15-day scorecard:
    HARD  the gate refuses, as its own code always did
    SOFT  the gate no longer refuses; the entry trades at half size (a size penalty of GATE_POLICY_SOFT_PENALTY, 1.0 = half)
    OFF   the gate does nothing (set by an operator only; the job never writes it)
The gates (keys are the gate_scorecard.py keys, so the same rows measure them):
    OPENING_GATE:GAP_DOWN                   the day opened more than OPENING_GATE_MAX_GAP_DOWN_PCT under the previous close
    OPENING_GATE:BELOW_OPEN                 price under the day's open
    OPENING_GATE:NEAR_DAY_HIGH              price in the top of the day's range (opening window)
    QUALITY_GATE:EVENT_CHECK_UNAVAILABLE    the news / earnings service did not answer and nothing is remembered
NEVER in this list, never adapted: circuit limits, liquidity, spread / depth, stale tick, capital, max positions, SHORT_TOO_EARLY, the 7% day-gain cap, and every
known-negative-event refusal (negative news, negative typed event, results today).

How a state is chosen (state()):
    GATE_POLICY_MODE=adaptive (default)  the row the job wrote in scalp_gate_policy; no row = the state the gate's own config gives (static_state()), i.e. today's behaviour
    GATE_POLICY_MODE=static              rows ignored: every gate behaves exactly as its config says (the rollback; also what a DB error gives)
    GATE_POLICY_MODE=all_hard            every adaptable gate refuses
The entry path only reads memory (the cache below); the DB is read by refresh() from the trading loop. Pure helpers; nothing here raises into a caller.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

import config

logger = logging.getLogger("position-stocks.gate_policy")

HARD, SOFT, OFF = "HARD", "SOFT", "OFF"
LONG = "LONG"

GAP_DOWN = "OPENING_GATE:GAP_DOWN"
BELOW_OPEN = "OPENING_GATE:BELOW_OPEN"
NEAR_DAY_HIGH = "OPENING_GATE:NEAR_DAY_HIGH"
EVENT = "QUALITY_GATE:EVENT_CHECK_UNAVAILABLE"

# gate -> (label, states a row may hold). EVENT has no OFF: "no news check at full size" is not something this policy ever allows.
GATES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    GAP_DOWN: ("Opening gap-down", (HARD, SOFT, OFF)),
    BELOW_OPEN: ("Price under the day open", (HARD, SOFT, OFF)),
    NEAR_DAY_HIGH: ("Opening: near the day high", (HARD, SOFT, OFF)),
    EVENT: ("News check unavailable", (HARD, SOFT)),
}

_cache: Dict[Tuple[str, str], str] = {}
_loaded_at: float = 0.0
_lock = threading.Lock()


def mode() -> str:
    """adaptive (default) | static | all_hard. Anything else reads as adaptive."""
    m = str(getattr(config, "GATE_POLICY_MODE", "adaptive") or "adaptive").strip().lower()
    return m if m in ("adaptive", "static", "all_hard") else "adaptive"


def static_state(gate: str) -> str:
    """The state the gate's own config gives today (what runs when no policy row exists). Never raises."""
    try:
        if gate == BELOW_OPEN:
            return HARD if str(getattr(config, "OPENING_BELOW_OPEN_MODE", "penalty") or "penalty").strip().lower() == "veto" else SOFT
        if gate == NEAR_DAY_HIGH:
            return SOFT if str(getattr(config, "OPENING_PARTS_MODE", "penalty") or "penalty").strip().lower() == "penalty" else HARD
        if gate == EVENT:
            return SOFT if bool(getattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", False)) else HARD
        return HARD                                   # GAP_DOWN (and anything unknown) refuses
    except Exception:  # noqa: BLE001
        return HARD


def state(gate: str, side: str = LONG) -> str:
    """The state in force for `gate` right now. Memory only, never blocks, never raises; any doubt = the config's own state."""
    try:
        m = mode()
        if m == "all_hard":
            return HARD
        if m == "adaptive":
            s = _cache.get((gate, side))
            allowed = GATES.get(gate, ("", ()))[1]
            if s in allowed:
                return s
        return static_state(gate)
    except Exception:  # noqa: BLE001
        return static_state(gate)


def soft_penalty() -> float:
    """Size penalty a SOFT gate adds (1.0 = half size: size / (1 + 1))."""
    try:
        return max(0.0, float(getattr(config, "GATE_POLICY_SOFT_PENALTY", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def event_half_size() -> bool:
    """True when a LONG whose news check could not be completed (nothing negative known) trades at half size instead of being refused."""
    return state(EVENT) == SOFT


def log_soft_rows() -> bool:
    return bool(getattr(config, "GATE_POLICY_LOG_SOFT_ROWS", True))


def set_cache(rows: Dict[Tuple[str, str], str]) -> None:
    """Replace the in-memory policy (the job and refresh() call this; tests too)."""
    global _cache, _loaded_at
    with _lock:
        _cache = {k: v for k, v in (rows or {}).items() if v in (HARD, SOFT, OFF)}
        _loaded_at = time.monotonic()


def clear_cache() -> None:
    set_cache({})


def snapshot() -> Dict[str, Any]:
    """{gate: {label, static, effective, row}} for /gate-policy and logs."""
    out: Dict[str, Any] = {}
    for g, (label, _allowed) in GATES.items():
        out[g] = {"label": label, "static": static_state(g), "effective": state(g), "row": _cache.get((g, LONG))}
    return out


def _stale() -> bool:
    try:
        return (time.monotonic() - _loaded_at) >= max(5.0, float(getattr(config, "GATE_POLICY_REFRESH_S", 60.0)))
    except (TypeError, ValueError):
        return True


def refresh(session_factory: Any) -> bool:
    """Reload the policy rows from scalp_gate_policy into the cache. False (cache kept) on any failure - a DB problem never changes a gate."""
    global _loaded_at
    s = None
    try:
        import models
        s = session_factory()
        rows = s.query(models.ScalpGatePolicy).all()
        # only rows that were MOVED (by the job or an operator) override a gate; the others just hold evidence and the gate keeps following its config
        set_cache({(r.gate, r.side or LONG): str(r.state or "").upper() for r in rows if r.last_change_day})
        return True
    except Exception as e:  # noqa: BLE001
        with _lock:
            _loaded_at = time.monotonic()               # do not hammer a failing DB every tick
        logger.warning("gate policy refresh failed (cache kept): %s", str(e)[:200])
        return False
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:  # noqa: BLE001
            pass


def refresh_due() -> bool:
    return mode() == "adaptive" and _stale()
