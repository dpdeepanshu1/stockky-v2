"""
Cross-service rate-limit reporting for Analysis Intelligence.

Writes to the same Neon keys the API Gateway Rate Limit Dashboard reads:
  stockky:rate_limit_stats
  stockky:rate_limit_events_neon

Also best-effort POSTs to gateway /ops/rate-limits/event when API_GATEWAY_URL is set.
Never raises to callers.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("rate-limit-report")

NEON_STATS_KEY = "stockky:rate_limit_stats"
NEON_EVENTS_KEY = "stockky:rate_limit_events_neon"
NEON_TTL = 86400


def _kv_get(key: str) -> Any:
    # Prefer local fundamental kv_cache when on path; fall back to import by path
    try:
        import kv_cache as _kv  # type: ignore
        return _kv.kv_get(key)
    except Exception:
        pass
    try:
        import sys
        base = os.path.dirname(os.path.abspath(__file__))
        fund = os.path.join(base, "fundamental")
        if fund not in sys.path:
            sys.path.insert(0, fund)
        import kv_cache as _kv  # type: ignore
        return _kv.kv_get(key)
    except Exception as e:
        logger.debug("rate_limit_report kv_get: %s", e)
        return None


def _kv_set(key: str, value: Any, ttl: int = NEON_TTL) -> None:
    try:
        import kv_cache as _kv  # type: ignore
        _kv.kv_set(key, value, ttl=ttl)
        return
    except Exception:
        pass
    try:
        import sys
        base = os.path.dirname(os.path.abspath(__file__))
        fund = os.path.join(base, "fundamental")
        if fund not in sys.path:
            sys.path.insert(0, fund)
        import kv_cache as _kv  # type: ignore
        _kv.kv_set(key, value, ttl=ttl)
    except Exception as e:
        logger.debug("rate_limit_report kv_set: %s", e)


def record_rate_limit_hit(
    provider: str = "analysis",
    status: int = 429,
    path: str = "",
    detail: str = "",
    symbol: str = "",
) -> None:
    """Push a 429/503 hit into Neon so the gateway dashboard is not blind."""
    src = (provider or "analysis").lower()[:40]
    now = time.time()
    event = {
        "ts": now,
        "source": src,
        "status": int(status),
        "path": (path or "")[:120],
        "detail": (detail or "")[:200],
        "symbol": (symbol or "")[:32],
        "origin": "analysis-intelligence-service",
    }
    try:
        raw_events = _kv_get(NEON_EVENTS_KEY)
        events: list = []
        if isinstance(raw_events, list):
            events = list(raw_events)
        elif isinstance(raw_events, dict) and isinstance(raw_events.get("events"), list):
            events = list(raw_events["events"])
        events.insert(0, event)
        events = events[:500]
        _kv_set(NEON_EVENTS_KEY, events, ttl=NEON_TTL)

        cutoff = now - 3600
        counts: Dict[str, int] = {}
        for e in events:
            if not isinstance(e, dict):
                continue
            if float(e.get("ts") or 0) < cutoff:
                continue
            s = str(e.get("source") or "unknown")
            counts[s] = counts.get(s, 0) + 1

        prior = _kv_get(NEON_STATS_KEY)
        if isinstance(prior, dict) and isinstance(prior.get("by_source_1h"), dict):
            for k, v in prior["by_source_1h"].items():
                if k not in counts:
                    try:
                        counts[k] = int(v)
                    except (TypeError, ValueError):
                        pass

        stats = {
            "updated_at": now,
            "window_sec": 3600,
            "by_source_1h": counts,
            "events_1h": sum(counts.values()),
            "limits": {
                "market_data": {"limit": 500},
                "analysis": {"limit": 300},
                "indianapi": {"limit": 250},
                "gemini": {"limit": 60},
                "groq": {"limit": 60},
                "nse": {"limit": 200},
            },
            "last_hit": event,
        }
        _kv_set(NEON_STATS_KEY, stats, ttl=NEON_TTL)
        logger.info("analysis rate_limit_hit provider=%s status=%s", src, status)
    except Exception as e:
        logger.warning("Failed to record analysis rate limit stat: %s", e)

    # Optional gateway in-process monitor
    try:
        import requests
        gw = os.environ.get("API_GATEWAY_URL", "").rstrip("/")
        if gw:
            requests.post(
                f"{gw}/ops/rate-limits/event",
                json={
                    "source": src,
                    "status": status,
                    "path": path,
                    "detail": str(detail)[:200],
                    "symbol": symbol,
                },
                timeout=2,
            )
    except Exception:
        pass


def report_if_rate_limited(
    err: Any = None,
    provider: str = "analysis",
    path: str = "",
    symbol: str = "",
    status: Optional[int] = None,
) -> bool:
    """
    Inspect an exception / message; if it looks like 429/503 rate-limit, record and return True.
    """
    msg = str(err or "")
    code = status
    if code is None:
        # Try httpx-style response
        try:
            resp = getattr(err, "response", None)
            if resp is not None and getattr(resp, "status_code", None):
                code = int(resp.status_code)
        except Exception:
            code = None
    low = msg.lower()
    is_rl = False
    if code in (429, 503):
        is_rl = True
    elif any(x in low for x in ("429", "too many", "rate limit", "rate-limited", "quota", "throttl")):
        is_rl = True
        code = code or 429
    if not is_rl:
        return False
    record_rate_limit_hit(
        provider=provider,
        status=int(code or 429),
        path=path,
        detail=msg[:200],
        symbol=symbol,
    )
    return True
