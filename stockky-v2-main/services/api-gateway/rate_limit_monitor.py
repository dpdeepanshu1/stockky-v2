"""
Rate-limit & upstream health monitor for free-tier Stockky.

Tracks:
  - HTTP 429 / 503 / 404 bursts from Yahoo, market-data, Gemini, Groq
  - Circuit breaker states
  - Rolling window counts (last 1h) in Redis when available, else process memory

Exposed via GET /ops/rate-limits on the API gateway.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("rate-limit-monitor")

WINDOW_SEC = 3600
MAX_EVENTS = 500
REDIS_KEY = "stockky:rate_limit_events"
REDIS_TTL = 86400
# Neon / kv_cache durable aggregate (survives Render worker restarts)
NEON_STATS_KEY = "stockky:rate_limit_stats"
NEON_EVENTS_KEY = "stockky:rate_limit_events_neon"
NEON_TTL = 86400

# Known upstreams shown on the dashboard
UPSTREAMS = [
    {"id": "market_data", "label": "Market Data / Yahoo", "codes": [429, 503, 404]},
    {"id": "gemini", "label": "Gemini LLM", "codes": [429]},
    {"id": "groq", "label": "Groq LLM", "codes": [429]},
    {"id": "indianapi", "label": "IndianAPI", "codes": [429, 403]},
    {"id": "nse", "label": "NSE official", "codes": [429, 403, 503]},
    {"id": "analysis", "label": "Analysis Intelligence", "codes": [502, 503]},
    {"id": "decision", "label": "Decision / Prediction", "codes": [502, 503]},
]


class RateLimitMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._events: Deque[dict] = deque(maxlen=MAX_EVENTS)
        self._redis = None
        self._neon_backed = False
        self._init_redis()
        self._load_neon_events()

    def _init_redis(self) -> None:
        # Default OFF — process memory only (dashboard still works in-process).
        # DISABLE_REDIS / DISABLE_UPSTASH force memory-only even if USE_REDIS=1.
        if os.environ.get("DISABLE_REDIS", "0").lower() in ("1", "true", "yes"):
            self._redis = None
            logger.info("RateLimitMonitor: memory-only (DISABLE_REDIS=1)")
            return
        if os.environ.get("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
            self._redis = None
            logger.info("RateLimitMonitor: memory-only (DISABLE_UPSTASH=1)")
            return
        if os.environ.get("USE_REDIS", "0").lower() not in ("1", "true", "yes"):
            self._redis = None
            logger.info("RateLimitMonitor: memory-only (USE_REDIS=0)")
            return
        url = os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            return
        try:
            from upstash_redis import Redis
            self._redis = Redis(url=url, token=token)
            self._redis.ping()
            logger.info("RateLimitMonitor using Redis")
        except Exception as e:
            logger.warning("RateLimitMonitor Redis unavailable: %s", e)
            self._redis = None



    def _kv_get(self, key: str):
        try:
            from kv_cache import kv_get
            return kv_get(key)
        except Exception as e:
            logger.debug("kv_get %s: %s", key, e)
            return None

    def _kv_set(self, key: str, value: Any, ttl: int = NEON_TTL) -> None:
        try:
            from kv_cache import kv_set
            kv_set(key, value, ttl=ttl)
        except Exception as e:
            logger.debug("kv_set %s: %s", key, e)

    def _load_neon_events(self) -> None:
        """Hydrate in-memory deque from Neon so dashboard survives worker restart."""
        try:
            raw = self._kv_get(NEON_EVENTS_KEY)
            if isinstance(raw, list) and raw:
                with self._lock:
                    for item in reversed(raw[-MAX_EVENTS:]):
                        if isinstance(item, dict) and item.get("ts"):
                            self._events.appendleft(item)
                self._neon_backed = True
                logger.info("RateLimitMonitor hydrated %s events from Neon", len(raw))
            elif isinstance(raw, dict) and isinstance(raw.get("events"), list):
                ev = raw["events"]
                with self._lock:
                    for item in reversed(ev[-MAX_EVENTS:]):
                        if isinstance(item, dict) and item.get("ts"):
                            self._events.appendleft(item)
                self._neon_backed = True
        except Exception as e:
            logger.debug("neon hydrate failed: %s", e)

    def _persist_neon(self) -> None:
        """Write rolling events + per-source counts to Neon (best-effort)."""
        try:
            with self._lock:
                events = list(self._events)[:MAX_EVENTS]
            self._kv_set(NEON_EVENTS_KEY, events, ttl=NEON_TTL)
            # Aggregate used counts by source (last window)
            cutoff = time.time() - WINDOW_SEC
            counts: Dict[str, int] = {}
            for e in events:
                if float(e.get("ts") or 0) < cutoff:
                    continue
                src = str(e.get("source") or "unknown")
                counts[src] = counts.get(src, 0) + 1
            stats = {
                "updated_at": time.time(),
                "window_sec": WINDOW_SEC,
                "by_source_1h": counts,
                "events_1h": sum(counts.values()),
                "limits": {
                    "market_data": {"limit": 500},
                    "indianapi": {"limit": 250},
                    "gemini": {"limit": 60},
                    "groq": {"limit": 60},
                    "nse": {"limit": 200},
                },
            }
            self._kv_set(NEON_STATS_KEY, stats, ttl=NEON_TTL)
            self._neon_backed = True
        except Exception as e:
            logger.debug("neon persist failed: %s", e)

    def record(
        self,
        source: str,
        status: int,
        path: str = "",
        detail: str = "",
        symbol: str = "",
    ) -> None:
        event = {
            "ts": time.time(),
            "source": (source or "unknown").lower()[:40],
            "status": int(status),
            "path": (path or "")[:120],
            "detail": (detail or "")[:200],
            "symbol": (symbol or "")[:32],
        }
        with self._lock:
            self._events.appendleft(event)
        if self._redis:
            try:
                self._redis.lpush(REDIS_KEY, json.dumps(event))
                self._redis.ltrim(REDIS_KEY, 0, MAX_EVENTS - 1)
                self._redis.expire(REDIS_KEY, REDIS_TTL)
            except Exception as e:
                logger.debug("Redis record failed: %s", e)
        # Durable Neon path (every event; cheap JSON write to stockky_kv)
        try:
            self._persist_neon()
        except Exception:
            pass

    def _all_events(self) -> List[dict]:
        events: List[dict] = []
        if self._redis:
            try:
                raw = self._redis.lrange(REDIS_KEY, 0, MAX_EVENTS - 1) or []
                for item in raw:
                    if isinstance(item, bytes):
                        item = item.decode()
                    if isinstance(item, str):
                        try:
                            events.append(json.loads(item))
                        except Exception:
                            continue
                    elif isinstance(item, dict):
                        events.append(item)
            except Exception as e:
                logger.debug("Redis read failed: %s", e)
        if not events:
            with self._lock:
                events = list(self._events)
        return events

    def snapshot(self, circuits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = time.time()
        events = self._all_events()
        recent = [e for e in events if now - float(e.get("ts") or 0) <= WINDOW_SEC]

        by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_status: Dict[str, int] = defaultdict(int)
        for e in recent:
            src = str(e.get("source") or "unknown")
            st = str(e.get("status") or 0)
            by_source[src][st] += 1
            by_status[st] += 1

        # Health labels per upstream
        upstream_status = []
        for u in UPSTREAMS:
            counts = by_source.get(u["id"], {})
            total_bad = sum(int(counts.get(str(c), 0)) for c in u["codes"])
            level = "ok"
            if total_bad >= 20:
                level = "critical"
            elif total_bad >= 5:
                level = "warn"
            elif total_bad >= 1:
                level = "watch"
            upstream_status.append({
                **u,
                "events_1h": total_bad,
                "by_status": dict(counts),
                "level": level,
            })

        # Overall
        total_429 = int(by_status.get("429", 0))
        total_503 = int(by_status.get("503", 0))
        if total_429 >= 15 or total_503 >= 30:
            overall = "critical"
        elif total_429 >= 5 or total_503 >= 10:
            overall = "degraded"
        elif total_429 or total_503:
            overall = "watch"
        else:
            overall = "healthy"

        circuit_list = []
        if circuits:
            for name, snap in circuits.items():
                circuit_list.append(snap if isinstance(snap, dict) else {"name": name, "raw": snap})

        # Merge Neon aggregate counts so dashboard is non-zero after restart
        neon_stats = self._kv_get(NEON_STATS_KEY) if hasattr(self, "_kv_get") else None
        neon_by = {}
        if isinstance(neon_stats, dict):
            neon_by = neon_stats.get("by_source_1h") or {}
            if not recent and neon_by:
                # Soft signal: at least show aggregate pressure after cold start
                pass

        return {
            "overall": overall,
            "window_sec": WINDOW_SEC,
            "events_1h": len(recent),
            "by_status_1h": dict(by_status),
            "by_source_1h": {**neon_by, **{u["id"]: sum(by_source.get(u["id"], {}).values()) for u in UPSTREAMS}},
            "upstreams": upstream_status,
            "circuits": circuit_list,
            "recent_events": recent[:40],
            "redis_backed": bool(self._redis),
            "neon_backed": bool(getattr(self, "_neon_backed", False) or neon_stats),
            "generated_at": now,
            "advice": _advice(overall, total_429, total_503),
        }


def _advice(overall: str, n429: int, n503: int) -> List[str]:
    tips = []
    if n429:
        tips.append("HTTP 429: slow scans, prefer Groq over Gemini, wait for cooldown.")
    if n503:
        tips.append("HTTP 503: free-tier cold start or Yahoo throttle — wake market-data, retry with backoff.")
    if overall == "healthy":
        tips.append("No rate-limit pressure in the last hour.")
    tips.append("Index names are mapped to Yahoo tickers — avoid raw 'NIFTY NEXT 50' paths.")
    return tips


monitor = RateLimitMonitor()
