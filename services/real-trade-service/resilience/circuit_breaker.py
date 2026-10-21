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

import asyncio
import logging
import os
import time
from typing import Any, Callable, Optional

from notifier import notify_async

logger = logging.getLogger("real-trade-circuit-breaker")


def _probe_alert_repeat_s() -> float:
    """group 485: seconds between repeated "probe failed again" Telegram alerts while a breaker stays down.
    RT_BREAKER_PROBE_ALERT_REPEAT_S (default 1800 = 30 min; 0 = alert on every failed probe, the old behaviour)."""
    try:
        return max(0.0, float(os.getenv("RT_BREAKER_PROBE_ALERT_REPEAT_S", "1800")))
    except (TypeError, ValueError):
        return 1800.0


def _alert(text: str) -> None:
    """
    Fire-and-forget Telegram alert on a breaker state change (2026-09-03
    durability upgrade). Degraded mode must never be silent — schedule the
    notification without blocking or failing the caller if the event loop
    or Telegram itself has a problem.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(notify_async(text))
    except RuntimeError:
        # No running loop (e.g. called from sync test code) — skip silently,
        # this is a best-effort alert, never a hard dependency.
        logger.debug("circuit_breaker: no running loop, skipping alert: %s", text)
    except Exception as exc:
        logger.warning("circuit_breaker: alert dispatch failed (%s)", exc)


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
        # group 485: alert throttling + the reason of the latest failure (so an alert says WHAT failed).
        self._last_error: Optional[str] = None
        self._probe_alerts_sent = 0
        self._last_probe_alert_at: Optional[float] = None

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

    def to_dict(self) -> dict:
        """Read-only status snapshot for the /resilience/status endpoint —
        2026-09-03. Never used for any trading decision, observability only."""
        if self._opened_at is None:
            state = "closed"
        elif self.is_open:
            state = "open"
        else:
            state = "half_open"
        return {
            "name": self.name,
            "state": state,
            "consecutive_failures": self._failures,
            "last_error": self._last_error,
            "failure_threshold": self.failure_threshold,
            "cooldown_s": self.cooldown_s,
            "seconds_until_retry": (
                max(0.0, self.cooldown_s - (time.monotonic() - self._opened_at))
                if self._opened_at is not None else None
            ),
        }

    def record_success(self) -> None:
        # BUG FIX (this session — "api-gateway recovered" alert firing with
        # no matching "DOWN" alert ever seen): this used to alert whenever
        # self._failures > 0, but the OPEN/"DOWN" alert in record_failure()
        # below only fires once _failures reaches failure_threshold (10).
        # A handful of ordinary transient failures (say, 2-9 in a row) that
        # then succeed is normal, expected network behaviour — the breaker
        # never actually opened, is_open never returned True, and no caller
        # ever fell back to degraded sourcing — but the old condition still
        # declared it "recovered", producing a confusing ✅ alert with no
        # preceding ⚠️ one. Only alert on an actual recovery: the breaker
        # had genuinely been OPEN (self._opened_at was set) and is now
        # closing again. Sub-threshold failure counts still reset to 0
        # below, silently, same as before — they just don't get announced.
        was_open = self._opened_at is not None
        if was_open:
            logger.info("circuit_breaker[%s]: recovered — CLOSED", self.name)
            _alert(f"✅ Stockky: {self.name} recovered — back to full signal quality (Tier 1).")
        self._failures = 0
        self._opened_at = None
        self._last_error = None
        self._probe_alerts_sent = 0
        self._last_probe_alert_at = None

    def record_failure(self, reason: Optional[str] = None) -> None:
        self._failures += 1
        if reason:
            self._last_error = str(reason)[:300]
        why = f" Last error: {self._last_error}." if self._last_error else ""
        if self._opened_at is None:
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit_breaker[%s]: OPEN after %d consecutive failures",
                    self.name, self._failures,
                )
                _alert(
                    f"⚠️ Stockky: {self.name} is DOWN ({self._failures} consecutive failures). "
                    f"Falling back to degraded signal sourcing for at least {self.cooldown_s:.0f}s.{why}"
                )
        else:
            # BUG FIX (2026-09-07): call() only reaches record_failure() while
            # self._opened_at is already set if is_open evaluated False —
            # i.e. the cooldown had elapsed and this was the single HALF-OPEN
            # probe call. Previously this branch didn't exist, so a failed
            # probe left _opened_at untouched at its original timestamp.
            # is_open computes `elapsed = now - self._opened_at`, and once
            # elapsed has already crossed cooldown_s once, it only keeps
            # growing — so every later check kept evaluating elapsed >=
            # cooldown_s and returning is_open=False (half-open) FOREVER,
            # regardless of how many more probes failed. That let every
            # subsequent call through as an unthrottled "probe" — exactly a
            # circuit breaker that's supposed to protect a struggling
            # upstream from being hammered, doing the opposite. It also
            # explains "half_open, seconds_until_retry=0" persisting
            # indefinitely on the dashboard: cooldown_s minus an
            # ever-growing elapsed floors at 0 and stays there. Re-arm the
            # clock here so a failed probe re-opens the breaker for another
            # full cooldown window, same as the initial trip.
            now = time.monotonic()
            self._opened_at = now
            logger.warning(
                "circuit_breaker[%s]: half-open probe failed (failure #%d) — "
                "re-OPENING for another %.0fs",
                self.name, self._failures, self.cooldown_s,
            )
            # group 485: this alert used to fire on EVERY failed probe (every cooldown_s), so one long outage sent a
            # Telegram message every 2 minutes (58 in a row on 2026-10-21). The first failed probe still alerts at once;
            # after that a reminder goes out at most once per RT_BREAKER_PROBE_ALERT_REPEAT_S. Logging is unchanged.
            gap = _probe_alert_repeat_s()
            due = (self._probe_alerts_sent == 0 or gap <= 0 or self._last_probe_alert_at is None
                   or (now - self._last_probe_alert_at) >= gap)
            if due:
                self._probe_alerts_sent += 1
                self._last_probe_alert_at = now
                _alert(
                    f"⚠️ Stockky: {self.name} probe failed again "
                    f"({self._failures} total consecutive failures) — still down, "
                    f"retrying in {self.cooldown_s:.0f}s.{why}"
                    + (f" (reminders every {gap / 60:.0f} min while it stays down)" if gap > 0 else "")
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
            self.record_failure(f"{type(exc).__name__}: {exc}".strip(": "))
            logger.warning(
                "circuit_breaker[%s]: call failed (%s) — using fallback",
                self.name, exc,
            )
            return await fallback() if fallback else None


# ── Module-level singletons ─────────────────────────────────────────────────
# One breaker per upstream dependency. Import these by name everywhere in
# real-trade-service instead of constructing new instances — a single shared
# instance is what makes the failure count accumulate correctly.

# 2026-09-09 calibration: raised failure_threshold 3→10 and cooldown to
# reduce Telegram alert spam. With threshold=3 and a 45s autopilot cycle,
# a single bad market-open window generated 60+ consecutive-failure alerts
# before the upstream recovered. Threshold=10 means the breaker still trips
# fast enough to protect the upstream but doesn't alert on every transient
# timeout storm. Cooldown raised to give cold-start free-tier services time
# to wake before the first probe fires.
api_gateway_breaker = CircuitBreaker(
    "api-gateway",
    failure_threshold=10,
    cooldown_s=120.0,
)

market_data_breaker = CircuitBreaker(
    "market-data-service",
    failure_threshold=10,
    cooldown_s=60.0,
)

event_service_breaker = CircuitBreaker(
    "event-service",
    failure_threshold=10,
    cooldown_s=90.0,
)
