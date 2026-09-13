"""
resilience/circuit_breaker.py — simple circuit breaker for this service.
Mirrors real-trade-service's own circuit_breaker pattern.
Trips on N consecutive failures, resets after RESET_TIMEOUT_S.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("position-stocks-circuit-breaker")

_FAILURE_THRESHOLD = 5
_RESET_TIMEOUT_S = 60.0

_failure_count = 0
_open_since: float = 0.0


def record_failure() -> None:
    global _failure_count, _open_since
    _failure_count += 1
    if _failure_count >= _FAILURE_THRESHOLD and _open_since == 0.0:
        _open_since = time.time()
        logger.error(
            "position-stocks circuit breaker OPEN after %d consecutive failures",
            _failure_count,
        )


def record_success() -> None:
    global _failure_count, _open_since
    _failure_count = 0
    _open_since = 0.0


def is_open() -> bool:
    global _failure_count, _open_since
    if _open_since == 0.0:
        return False
    if time.time() - _open_since > _RESET_TIMEOUT_S:
        logger.info("position-stocks circuit breaker: reset timeout elapsed — half-open")
        _open_since = 0.0
        _failure_count = 0
        return False
    return True


def status() -> dict:
    return {
        "open": is_open(),
        "failure_count": _failure_count,
        "open_since": _open_since or None,
    }
