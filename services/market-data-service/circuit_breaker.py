"""
Lightweight in-process circuit breaker for free-tier microservice calls.

States:
  closed     — normal traffic
  open       — fail fast after consecutive failures (no long timeouts)
  half_open  — allow a probe after recovery_timeout

No external dependencies. One breaker per downstream name (process-local).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("circuit-breaker")

# Defaults tuned for Render free tier: open quickly, recover in ~1–2 min
DEFAULT_FAILURE_THRESHOLD = 4
DEFAULT_RECOVERY_TIMEOUT = 90.0
DEFAULT_HALF_OPEN_SUCCESS = 1


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"circuit open for {name}; retry after {retry_after:.0f}s")


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_success: int = DEFAULT_HALF_OPEN_SUCCESS,
    ):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_success = max(1, int(half_open_success))
        self._lock = threading.Lock()
        self._failures = 0
        self._successes_half = 0
        self._state = "closed"  # closed | open | half_open
        self._opened_at = 0.0
        self._last_error: Optional[str] = None

    def state(self) -> str:
        with self._lock:
            self._maybe_half_open_unlocked()
            return self._state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._maybe_half_open_unlocked()
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "opened_at": self._opened_at or None,
                "last_error": self._last_error,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
            }

    def _maybe_half_open_unlocked(self) -> None:
        if self._state == "open":
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.recovery_timeout:
                self._state = "half_open"
                self._successes_half = 0
                logger.info("circuit %s → half_open after %.0fs", self.name, elapsed)

    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open_unlocked()
            if self._state == "open":
                return False
            return True

    def retry_after(self) -> float:
        with self._lock:
            if self._state != "open":
                return 0.0
            left = self.recovery_timeout - (time.monotonic() - self._opened_at)
            return max(0.0, left)

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._successes_half += 1
                if self._successes_half >= self.half_open_success:
                    self._state = "closed"
                    self._failures = 0
                    self._last_error = None
                    logger.info("circuit %s → closed (recovered)", self.name)
            else:
                self._failures = 0
                self._state = "closed"

    def record_failure(self, err: str = None) -> None:
        with self._lock:
            self._last_error = (err or "")[:200]
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                self._failures = self.failure_threshold
                logger.warning("circuit %s → open (half_open probe failed): %s", self.name, self._last_error)
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit %s → open after %s failures: %s",
                    self.name,
                    self._failures,
                    self._last_error,
                )

    def call(self, func: Callable, *args, **kwargs):
        """Sync call through the breaker."""
        if not self.allow():
            raise CircuitOpenError(self.name, self.retry_after())
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e))
            raise


# Process-wide registry
_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
) -> CircuitBreaker:
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CircuitBreaker(
                name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _registry[name]


def all_snapshots() -> Dict[str, dict]:
    with _registry_lock:
        return {k: v.snapshot() for k, v in _registry.items()}
