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


def _get_redis_client():
    """Uses upstash_redis.Redis (REST-based), matching what api-gateway
    and scheduler-service actually use in this codebase — confirmed by
    reading api-gateway's source, not guessed. Needs UPSTASH_REDIS_REST_URL
    and UPSTASH_REDIS_REST_TOKEN, the same pair those services already use,
    not a separate REDIS_URL."""
    from upstash_redis import Redis
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set — required "
            "for indianapi_fallback's cache and rate limiter. Same credentials "
            "api-gateway and scheduler-service already use."
        )
    return Redis(url=url, token=token)


def _cache_get(redis_client, symbol: str) -> Optional[Dict[str, Any]]:
    raw = redis_client.get(CACHE_KEY_PREFIX + symbol.upper())
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _cache_set(redis_client, symbol: str, payload: Dict[str, Any]) -> None:
    # No Redis TTL set deliberately — expiry is trading-day-aware
    # (_is_cache_fresh), not a flat wall-clock TTL, so we manage
    # expiry ourselves in the stored `cached_at` field rather than
    # relying on Redis to evict it.
    redis_client.set(CACHE_KEY_PREFIX + symbol.upper(), json.dumps(payload))


def _add_trading_days(start: date, n: int) -> date:
    """Skips Sat/Sun. Does NOT know NSE holidays (no holiday calendar
    available) — worst case this treats an NSE holiday as a trading day,
    making the cache refresh very slightly earlier than strictly
    necessary. That's a safe direction to be wrong in for a rate-limited
    free-tier budget: it costs at most one extra call around a holiday,
    it never under-refreshes."""
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _cache_expiry(cached_at: datetime) -> datetime:
    expiry_date = _add_trading_days(cached_at.astimezone(IST).date(), CACHE_TRADING_DAYS)
    return datetime.combine(expiry_date, NSE_MARKET_OPEN, tzinfo=IST)


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
