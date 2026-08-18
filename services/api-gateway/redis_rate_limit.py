"""
Redis sliding-window rate limiter for free-tier multi-service Stockky.

Each Render service is a separate account/dyno (own 512MB). Rate limits protect
*shared* upstreams (Yahoo via market-data, Gemini, etc.) without shrinking the
scan universe — we only pace outbound calls.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("redis-rate-limit")

# Defaults tuned for separate free dynos + Yahoo softness
DEFAULT_LIMITS = {
    "market_data": (int(os.getenv("RL_MARKET_DATA_RPM", "45")), 60),
    "analysis": (int(os.getenv("RL_ANALYSIS_RPM", "60")), 60),
    "decision": (int(os.getenv("RL_DECISION_RPM", "80")), 60),
    "gemini": (int(os.getenv("RL_GEMINI_RPM", "12")), 60),
    "global": (int(os.getenv("RL_GLOBAL_RPM", "120")), 60),
}


class RedisRateLimiter:
    def __init__(self, redis_client=None):
        self._redis = redis_client

    def set_redis(self, redis_client) -> None:
        self._redis = redis_client

    def allow(self, bucket: str, cost: int = 1) -> bool:
        """
        Return True if request may proceed.
        Fail-open (allow) if Redis is down so scans never stall hard.
        """
        limit, window = DEFAULT_LIMITS.get(bucket, DEFAULT_LIMITS["global"])
        if self._redis is None:
            return True
        key = f"stockky:rl:{bucket}:{int(time.time() // window)}"
        try:
            n = self._redis.incrby(key, cost)
            if n == cost or n == 1:
                try:
                    self._redis.expire(key, int(window) + 2)
                except Exception:
                    pass
            if n > limit:
                logger.info("rate limit hit bucket=%s n=%s limit=%s", bucket, n, limit)
                return False
            return True
        except Exception as e:
            logger.debug("rate limit redis error (fail-open): %s", e)
            return True

    def wait_budget_sec(self, bucket: str) -> float:
        """Suggested sleep when limited (remaining window seconds)."""
        _, window = DEFAULT_LIMITS.get(bucket, DEFAULT_LIMITS["global"])
        return float(window - (time.time() % window)) + 0.05


# Process singleton; wire redis in gateway startup
limiter = RedisRateLimiter()
