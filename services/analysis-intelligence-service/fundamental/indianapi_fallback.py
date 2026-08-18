"""
IndianAPI fundamentals fallback — used ONLY when Yahoo Finance fails to
return data for a symbol. Never called as a primary source; yfinance
stays primary everywhere it already is.

Caches whatever IndianAPI returns for 5 trading days per symbol, so a
symbol Yahoo keeps failing on doesn't get re-fetched from IndianAPI on
every scan. Cache validity boundary is NSE market open (9:15 IST) on the
6th trading day after it was cached — not a flat 5*24h TTL, so the cache
doesn't expire mid-day at an arbitrary wall-clock time.

Rate-limited to 1 request/second via a Redis-backed timestamp, so it's
safe even if multiple worker processes/replicas call this concurrently —
a plain in-process sleep() wouldn't coordinate across processes.

Verified against IndianAPI's actual public docs (https://indianapi.in/
indian-stock-market, https://indianapi.in/documentation/indian-stock-market):
  Base URL:  https://stock.indianapi.in
  Endpoint:  GET /stock?name={company_name_or_symbol}
  Auth:      header "x-api-key: YOUR_KEY"
  Response:  tickerId, companyName, currentPrice {BSE, NSE}, financials,
             keyMetrics, stockTechnicalData, percentChange, yearHigh, yearLow

The exact field names inside `financials`/`keyMetrics` weren't in the
public docs snippet available at build time — this module returns them
as-is (raw dict) rather than guessing a mapping to specific ratio names
like debt_to_equity/roe. Confirm those field names against a live
response before wiring specific values into any scoring logic.
"""
import os
import time
import json
import logging
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Callable

import requests

logger = logging.getLogger("fundamental-analysis-service.indianapi_fallback")

IST = ZoneInfo("Asia/Kolkata")
NSE_MARKET_OPEN = dtime(9, 15)

INDIANAPI_BASE_URL = "https://stock.indianapi.in"
INDIANAPI_KEY = os.environ.get("INDIANAPI_KEY")

CACHE_KEY_PREFIX = "indianapi:fundamentals:"
CACHE_TRADING_DAYS = 5

RATE_LIMIT_KEY = "indianapi:last_request_ts"
MIN_REQUEST_INTERVAL_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 10



# ── Memory-first cache (USE_REDIS=0 default — stops Upstash burn during data-feed) ──
_USE_REDIS = os.environ.get("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.environ.get("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    _USE_REDIS = False

_MEM_CACHE: Dict[str, Any] = {}
_MEM_LAST_TS: float = 0.0
_redis_client = None
_redis_init = False


def _get_redis_client():
    """Optional Upstash only when USE_REDIS=1. Default: None (memory only)."""
    global _redis_client, _redis_init
    if not _USE_REDIS:
        return None
    if _redis_init:
        return _redis_client
    _redis_init = True
    try:
        from upstash_redis import Redis
        url = os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            logger.info("IndianAPI: USE_REDIS=1 but no Upstash credentials — memory only")
            _redis_client = None
            return None
        _redis_client = Redis(url=url, token=token)
        logger.info("IndianAPI: Upstash Redis ON (USE_REDIS=1)")
    except Exception as e:
        logger.warning("IndianAPI Redis unavailable: %s — memory only", e)
        _redis_client = None
    return _redis_client


def _cache_get(redis_client, symbol: str) -> Optional[Dict[str, Any]]:
    key = CACHE_KEY_PREFIX + symbol.upper()
    # memory first
    hit = _MEM_CACHE.get(key)
    if hit is not None:
        return hit if isinstance(hit, dict) else None
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(key)
        if raw is None:
            return None
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if isinstance(data, dict):
            _MEM_CACHE[key] = data
        return data
    except Exception:
        return None


def _cache_set(redis_client, symbol: str, payload: Dict[str, Any]) -> None:
    key = CACHE_KEY_PREFIX + symbol.upper()
    _MEM_CACHE[key] = payload
    # Soft cap memory
    if len(_MEM_CACHE) > 2000:
        for k in list(_MEM_CACHE.keys())[:200]:
            _MEM_CACHE.pop(k, None)
    if redis_client is None:
        return
    try:
        redis_client.set(key, json.dumps(payload, default=str))
    except Exception as e:
        logger.debug("IndianAPI redis set skip: %s", e)


def _rate_limit_wait(redis_client) -> None:
    """In-process 1 req/sec. Redis path only if USE_REDIS=1."""
    global _MEM_LAST_TS
    import time as _t
    now = _t.time()
    wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _MEM_LAST_TS)
    if wait > 0:
        _t.sleep(wait)
    _MEM_LAST_TS = _t.time()
    if redis_client is None:
        return
    try:
        last = redis_client.get(RATE_LIMIT_KEY)
        if last is not None:
            try:
                last_f = float(last)
                gap = MIN_REQUEST_INTERVAL_SECONDS - (_t.time() - last_f)
                if gap > 0:
                    _t.sleep(gap)
            except Exception:
                pass
        redis_client.set(RATE_LIMIT_KEY, str(_t.time()))
    except Exception:
        pass


def _is_cache_fresh(cached_payload: Dict[str, Any]) -> bool:
    try:
        cached_at = datetime.fromisoformat(cached_payload["cached_at"])
    except (KeyError, ValueError):
        return False
    return datetime.now(IST) < _cache_expiry(cached_at)


def _enforce_rate_limit(redis_client) -> None:
    """Blocks the calling thread until at least 1 second has passed since
    the last real IndianAPI call from ANY process sharing this Redis
    instance. Not perfectly race-free under concurrent callers (two
    processes could both pass the check within the same tiny window),
    but for a single shared free-tier API key the goal is "don't burst",
    not strict mutual exclusion — good enough for that."""
    while True:
        last_ts = redis_client.get(RATE_LIMIT_KEY)
        now = time.time()
        if last_ts is None or (now - float(last_ts)) >= MIN_REQUEST_INTERVAL_SECONDS:
            redis_client.set(RATE_LIMIT_KEY, str(now))
            return
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - (now - float(last_ts)) + 0.01)


def _fetch_from_indianapi(symbol: str) -> Optional[Dict[str, Any]]:
    if not INDIANAPI_KEY:
        logger.warning("INDIANAPI_KEY not set — cannot use IndianAPI fallback for %s", symbol)
        return None
    try:
        response = requests.get(
            f"{INDIANAPI_BASE_URL}/stock",
            params={"name": symbol},
            headers={"x-api-key": INDIANAPI_KEY},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error("IndianAPI request failed for %s: %s", symbol, e)
        return None


def get_fundamentals_with_fallback(
    symbol: str,
    yahoo_fetch_fn: Callable[[str], Optional[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """
    Primary path is always yahoo_fetch_fn(symbol) — pass in whatever
    function already wraps yfinance in this service, e.g.:

        from indianapi_fallback import get_fundamentals_with_fallback
        data = get_fundamentals_with_fallback(symbol, fetch_yahoo_fundamentals)

    Only calls IndianAPI (rate-limited, cached) if yahoo_fetch_fn raises
    or returns None/empty. Returns None if both sources fail.
    """
    try:
        yahoo_result = yahoo_fetch_fn(symbol)
        if yahoo_result:
            return yahoo_result
        logger.info("Yahoo Finance returned no data for %s — trying IndianAPI fallback", symbol)
    except Exception as e:
        logger.warning("Yahoo Finance fetch failed for %s (%s) — trying IndianAPI fallback", symbol, e)

    try:
        redis_client = _get_redis_client()
    except RuntimeError as e:
        logger.error(str(e))
        return None

    cached = _cache_get(redis_client, symbol)
    if cached is not None and _is_cache_fresh(cached):
        logger.info("Using cached IndianAPI data for %s (cached_at=%s)", symbol, cached.get("cached_at"))
        return cached["data"]

    _enforce_rate_limit(redis_client)
    fresh_data = _fetch_from_indianapi(symbol)
    if fresh_data is None:
        if cached is not None:
            logger.warning(
                "IndianAPI call failed for %s — serving stale cached data (better than nothing) "
                "cached_at=%s", symbol, cached.get("cached_at")
            )
            return cached["data"]
        return None

    _cache_set(redis_client, symbol, {
        "data": fresh_data,
        "cached_at": datetime.now(IST).isoformat(),
    })
    return fresh_data
