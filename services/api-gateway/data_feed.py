"""
Data Feed — durable cache of slow-changing fields (12–24h).

Purpose: free-tier rate-limit relief. Real-time paths (quote, decide, scan)
reuse fundamentals / sector / peer / multi-quarter / static event snapshot
from this store instead of hitting upstream APIs every time.

NOT stored here (always live when needed):
  - last price / quote
  - intraday technicals
  - brand-new headlines (optional short news summary may be refreshed)

Stored (stable ≥ hours):
  - fundamental score + metrics + sector + valuation + peers
  - multi-quarter / quality scores
  - industry, company name
  - semi-static event snapshot (bulk/insider/earnings surprise)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("data-feed")

IST = timezone(timedelta(hours=5, minutes=30))

DATA_FEED_PREFIX = "stockky:data_feed:sym:"
DATA_FEED_META_KEY = "stockky:data_feed:meta"
DATA_FEED_JOB_KEY = "stockky:data_feed:job"
DATA_FEED_TTL = int(os.getenv("DATA_FEED_TTL_SECONDS", str(20 * 3600)))  # ~20h


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def extract_feed_payload(
    symbol: str,
    fundamental: Optional[dict] = None,
    events: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> Dict[str, Any]:
    """Normalize slow fields from upstream responses."""
    f = fundamental if isinstance(fundamental, dict) else {}
    e = events if isinstance(events, dict) else {}
    metrics = f.get("metrics") if isinstance(f.get("metrics"), dict) else {}
    payload = {
        "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
        "updated_at": _now_iso(),
        "fundamental_score": f.get("fundamental_score"),
        "valuation": f.get("valuation"),
        "sector": f.get("sector"),
        "industry": f.get("industry"),
        "peer_relative_score": f.get("peer_relative_score"),
        "peer_relative": f.get("peer_relative"),
        "peer_list": f.get("peer_list"),
        "multi_quarter_score": f.get("multi_quarter_score"),
        "multi_quarter_ok": f.get("multi_quarter_ok"),
        "multi_quarter_detail": f.get("multi_quarter_detail"),
        "quality_score": f.get("quality_score"),
        "metrics": metrics,
        "fundamental_reasons": (f.get("reasons") or [])[:6],
        "fallback_used": f.get("fallback_used"),
        # Semi-static event snapshot
        "bulk_deals": (e.get("bulk_deals") or [])[:5],
        "recent_insider_transactions": (e.get("recent_insider_transactions") or [])[:5],
        "earnings_surprise": e.get("earnings_surprise"),
        "next_earnings_date": e.get("next_earnings_date"),
        "event_summary": e.get("event_summary") or e.get("summary"),
        "has_positive_catalyst": e.get("has_positive_catalyst"),
        "recent_event_score": e.get("recent_event_score"),
    }
    if extra and isinstance(extra, dict):
        payload.update(extra)
    return payload


class DataFeedStore:
    def __init__(self, redis_get, redis_set, redis_client=None):
        self._get = redis_get
        self._set = redis_set
        self._redis = redis_client

    def get_symbol(self, symbol: str) -> Optional[dict]:
        key = DATA_FEED_PREFIX + symbol.upper().replace(".NS", "").replace(".BO", "")
        val = self._get(key)
        return val if isinstance(val, dict) else None

    def put_symbol(self, symbol: str, payload: dict, ttl: int = DATA_FEED_TTL) -> None:
        key = DATA_FEED_PREFIX + symbol.upper().replace(".NS", "").replace(".BO", "")
        self._set(key, payload, ttl=ttl)

    def meta(self) -> dict:
        m = self._get(DATA_FEED_META_KEY)
        return m if isinstance(m, dict) else {
            "last_success_at": None,
            "last_count": 0,
            "last_message": "No data feed run yet",
            "source": None,
        }

    def set_meta(self, **kwargs) -> dict:
        m = self.meta()
        m.update(kwargs)
        m["updated_at"] = _now_iso()
        self._set(DATA_FEED_META_KEY, m, ttl=7 * 86400)
        return m

    def job(self) -> dict:
        j = self._get(DATA_FEED_JOB_KEY)
        return j if isinstance(j, dict) else {
            "status": "idle",
            "processed": 0,
            "total": 0,
            "started_at": None,
            "elapsed_sec": 0,
            "estimated_remaining_sec": None,
            "message": "Idle",
        }

    def set_job(self, **kwargs) -> dict:
        j = self.job()
        j.update(kwargs)
        if j.get("started_at") and j.get("status") == "running":
            try:
                started = datetime.fromisoformat(str(j["started_at"]))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=IST)
                j["elapsed_sec"] = int((datetime.now(IST) - started).total_seconds())
                done = int(j.get("processed") or 0)
                total = max(int(j.get("total") or 1), 1)
                if done > 0:
                    rate = j["elapsed_sec"] / done
                    j["estimated_remaining_sec"] = int(rate * (total - done))
            except Exception:
                pass
        self._set(DATA_FEED_JOB_KEY, j, ttl=86400)
        return j


# Hot-picks job keys (on-demand run)
HOT_JOB_KEY = "stockky:hot_job"
HOT_RESULT_KEY = "stockky:hot_result_db"


def hot_job_get(redis_get) -> dict:
    j = redis_get(HOT_JOB_KEY)
    return j if isinstance(j, dict) else {
        "status": "idle",
        "processed": 0,
        "total": 0,
        "started_at": None,
        "elapsed_sec": 0,
        "estimated_remaining_sec": None,
        "message": "Idle — click Search Hot Picks Stocks",
    }


def hot_job_set(redis_set, redis_get, **kwargs) -> dict:
    j = hot_job_get(redis_get)
    j.update(kwargs)
    if j.get("started_at") and j.get("status") == "running":
        try:
            started = datetime.fromisoformat(str(j["started_at"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=IST)
            j["elapsed_sec"] = int((datetime.now(IST) - started).total_seconds())
            done = int(j.get("processed") or 0)
            total = max(int(j.get("total") or 1), 1)
            if done > 0:
                j["estimated_remaining_sec"] = int((j["elapsed_sec"] / done) * (total - done))
        except Exception:
            pass
    redis_set(HOT_JOB_KEY, j, ttl=86400)
    return j


# ── Cache stampede protection ─────────────────────────────────────────────
LOCK_PREFIX = "stockky:lock:refresh:"


def try_refresh_lock(redis_client, symbol: str, ttl_sec: int = 5) -> bool:
    """
    Distributed mutex: only one worker refreshes a ticker at a time.
    Returns True if this caller holds the lock (should fetch upstream).
    Upstash Redis: SET key value NX EX ttl
    """
    if redis_client is None:
        return True
    key = f"{LOCK_PREFIX}{(symbol or '').upper()}"
    try:
        # upstash-redis supports set with ex + nx
        ok = redis_client.set(key, "1", nx=True, ex=int(ttl_sec))
        return bool(ok)
    except TypeError:
        try:
            # fallback signature
            ok = redis_client.set(key, "1", ex=ttl_sec, nx=True)
            return bool(ok)
        except Exception as e:
            logger.debug("refresh lock unavailable: %s", e)
            return True
    except Exception as e:
        logger.debug("refresh lock error: %s", e)
        return True


def release_refresh_lock(redis_client, symbol: str) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(f"{LOCK_PREFIX}{(symbol or '').upper()}")
    except Exception:
        pass


def soft_ttl_should_refresh(redis_client, key: str, soft_window: int = 10) -> bool:
    """True when key is within soft_window seconds of expiry."""
    if redis_client is None:
        return False
    try:
        ttl = redis_client.ttl(key)
        return isinstance(ttl, int) and 0 < ttl <= soft_window
    except Exception:
        return False
