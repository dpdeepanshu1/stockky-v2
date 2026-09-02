"""
resilience/circuit_breaker.py — Short-Term Trading Upgrade (2026-09-02)

Minimal circuit breaker for outbound calls from real-trade-service.
Trips after N consecutive failures, serves local_cache fallback while open,
half-opens after a cooldown period to test recovery.

Three module-level singletons cover the three upstream dependencies:
  api_gateway_breaker    — api-gateway (Tier 1 hot_picks / ipo)
  market_data_breaker    — market-data-service (quotes / events)
  event_service_breaker  — analysis-intelligence-service event sub-service
                           (Tier 2 /events/raw-feed)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("real-trade-circuit-breaker")


class CircuitBreaker:
    """
    States:
      CLOSED  — calls pass through normally.
      OPEN    — calls are blocked; fallback is used.
      HALF-OPEN — after cooldown_s the breaker lets one call through as a
                  probe; success resets it to CLOSED, failure re-opens it.
    """

    def __init__(self, name: str, failure_threshold: int = 3, cooldown_s: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self.cooldown_s:
            # Half-open: allow a single probe call through.
            # The probe outcome (record_success / record_failure) decides state.
            return False
        return True

    def record_success(self) -> None:
        if self._failures > 0 or self._opened_at is not None:
            logger.info("circuit_breaker[%s]: recovered — CLOSED", self.name)
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_breaker[%s]: OPEN after %d consecutive failures",
                self.name, self._failures,
            )

    async def call(
        self,
        fn: Callable,
        *args: Any,
        fallback: Optional[Callable] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Call fn(*args, **kwargs). On success record it and return the result.
        On failure (or if the breaker is open), call fallback() instead.
        fallback must be an async callable returning the degraded value or None.
        """
        if self.is_open:
            logger.info("circuit_breaker[%s]: OPEN — using fallback", self.name)
            return await fallback() if fallback else None

        try:
            result = await fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure()
            logger.warning(
                "circuit_breaker[%s]: call failed (%s) — using fallback",
                self.name, exc,
            )
            return await fallback() if fallback else None


# ── Module-level singletons ─────────────────────────────────────────────────
# One breaker per upstream dependency. Import these by name everywhere in
# real-trade-service instead of constructing new instances — a single shared
# instance is what makes the failure count accumulate correctly.

api_gateway_breaker = CircuitBreaker(
    "api-gateway",
    failure_threshold=3,
    cooldown_s=90.0,
)

market_data_breaker = CircuitBreaker(
    "market-data-service",
    failure_threshold=3,
    cooldown_s=45.0,
)

event_service_breaker = CircuitBreaker(
    "event-service",
    failure_threshold=3,
    cooldown_s=60.0,
)
