"""
In-memory sliding-window / token-bucket rate limiter for free-tier Stockky.

Replaces any external Redis dependency. Works entirely inside the Render
container process. Safe for a single free-tier dyno (512 MB).

Buckets are independent (market_data, analysis, decision, gemini, global).
Fail-open is implicit: if something goes wrong we allow the request.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Dict, List

logger = logging.getLogger("redis-rate-limit")

# Defaults tuned for separate free dynos + Yahoo softness
DEFAULT_LIMITS = {
    "market_data": (int(os.getenv("RL_MARKET_DATA_RPM", "45")), 60),
    "analysis": (int(os.getenv("RL_ANALYSIS_RPM", "60")), 60),
    "decision": (int(os.getenv("RL_DECISION_RPM", "80")), 60),
    "gemini": (int(os.getenv("RL_GEMINI_RPM", "12")), 60),
    "global": (int(os.getenv("RL_GLOBAL_RPM", "120")), 60),
}


class LocalMemoryRateLimiter:
    """
    Sliding-window rate limiter stored in process memory.
    No Redis, no network, no cold-start.
    """

    def __init__(self, requests_per_minute: int = 120):
        self.rpm = requests_per_minute
        # client_id / bucket → list of timestamps
        self._tokens: Dict[str, List[float]] = defaultdict(list)
        self._limits = dict(DEFAULT_LIMITS)

    def set_redis(self, redis_client) -> None:
        """
        Compatibility shim. Previously wired a Redis client.
        Now ignored — we stay purely in-memory.
        """
        if redis_client is not None:
            logger.info(
                "LocalMemoryRateLimiter: ignoring injected Redis client "
                "(USE_REDIS paths disabled by design)"
            )

    def allow(self, bucket: str, cost: int = 1) -> bool:
        """
        Return True if the request may proceed.
        Always fail-open on any internal error.
        """
        try:
            limit, window = self._limits.get(bucket, self._limits["global"])
            now = time.time()
            cutoff = now - float(window)
            key = bucket

            # Prune old timestamps
            stamps = [t for t in self._tokens[key] if t > cutoff]
            self._tokens[key] = stamps

            if len(stamps) + cost <= limit:
                for _ in range(cost):
                    self._tokens[key].append(now)
                return True

            logger.info(
                "rate limit hit bucket=%s n=%s limit=%s",
                bucket,
                len(stamps),
                limit,
            )
            return False
        except Exception as e:
            logger.debug("rate limit error (fail-open): %s", e)
            return True

    def is_allowed(self, client_id: str) -> bool:
        """
        Simple per-client API matching the sketch in the performance plan.
        Uses the global bucket.
        """
        return self.allow(client_id or "global", cost=1)

    def wait_budget_sec(self, bucket: str) -> float:
        """Suggested sleep when limited (remaining window seconds)."""
        _, window = self._limits.get(bucket, self._limits["global"])
        return float(window - (time.time() % window)) + 0.05


# Process singleton — same name as before so main.py imports stay valid
limiter = LocalMemoryRateLimiter()
rate_limiter = limiter  # alias used by some call sites
