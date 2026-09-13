"""
resilience/circuit_breaker.py — simple circuit breaker for this service.
Mirrors real-trade-service's own circuit_breaker pattern.
Trips on N consecutive failures, half-opens after RESET_TIMEOUT_S (a single
probe call is let through), and either closes again (probe succeeds) or
re-opens for another full cooldown (probe fails).

BUG FIX (session13 audit): status() previously returned {open, failure_count,
open_since} but the frontend (PositionStocksTab.tsx's CBadge component, typed
in positionStocksApi.ts) has always expected {state, consecutive_failures,
failure_threshold, cooldown_s, seconds_until_retry} — the exact shape
real-trade-service's own CircuitBreaker.to_dict() returns. Field-name
mismatch meant the circuit breaker badge on the dashboard rendered "—" (the
`if (!cb) return <span>—</span>` fallback, since every field it reads was
undefined) since the tab was built — never the actual breaker state. Same
class of bug as the WS status mismatch fixed in the same session.

Also fixed while rewriting: the old is_open() silently reset _failure_count
and _open_since to 0 the moment the cooldown elapsed, collapsing straight to
CLOSED instead of a real HALF-OPEN probe state — so a failed probe call
right after cooldown started counting failures from zero again instead of
re-arming the open state, and half_open was never a state this module could
actually report. Rewritten to keep _open_since set through the half-open
window and only clear it on record_success(); a failure recorded while
half-open re-arms the clock for another full cooldown, matching
real-trade-service's documented fix for the exact same bug shape.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("position-stocks-circuit-breaker")

_FAILURE_THRESHOLD = 5
_RESET_TIMEOUT_S = 60.0

_failure_count = 0
_open_since: float = 0.0  # monotonic timestamp the breaker tripped; 0.0 = never tripped / fully closed


def _elapsed_since_open() -> float:
    return time.monotonic() - _open_since if _open_since else 0.0


def record_failure() -> None:
    global _failure_count, _open_since
    _failure_count += 1
    if _open_since == 0.0:
        if _failure_count >= _FAILURE_THRESHOLD:
            _open_since = time.monotonic()
            logger.error(
                "position-stocks circuit breaker OPEN after %d consecutive failures",
                _failure_count,
            )
    else:
        # A failure recorded while _open_since is already set only happens
        # once the cooldown has elapsed (is_open() lets the single
        # half-open probe call through) — that probe failed, so re-arm the
        # clock for another full cooldown instead of leaving the original,
        # now-stale timestamp in place (which would make is_open() keep
        # returning False forever — the bug real-trade-service already hit
        # and fixed for its own breaker).
        _open_since = time.monotonic()
        logger.warning(
            "position-stocks circuit breaker: half-open probe failed (failure #%d) — "
            "re-OPENING for another %.0fs",
            _failure_count, _RESET_TIMEOUT_S,
        )


def record_success() -> None:
    global _failure_count, _open_since
    _failure_count = 0
    _open_since = 0.0


def is_open() -> bool:
    """True while calls should be blocked. False once the cooldown has
    elapsed — that also means the very next call is the half-open probe;
    its outcome (record_success/record_failure) decides the next state."""
    if _open_since == 0.0:
        return False
    return _elapsed_since_open() < _RESET_TIMEOUT_S


def status() -> dict:
    if _open_since == 0.0:
        state = "closed"
    elif is_open():
        state = "open"
    else:
        state = "half_open"
    return {
        "state": state,
        "consecutive_failures": _failure_count,
        "failure_threshold": _FAILURE_THRESHOLD,
        "cooldown_s": _RESET_TIMEOUT_S,
        "seconds_until_retry": (
            max(0.0, _RESET_TIMEOUT_S - _elapsed_since_open()) if _open_since else None
        ),
    }
