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

try:
    import kv_cache as _kv
except Exception:
    _kv = None  # type: ignore
_MEM_LAST_TS = 0.0


IST = ZoneInfo("Asia/Kolkata")
NSE_MARKET_OPEN = dtime(9, 15)

INDIANAPI_BASE_URL = "https://stock.indianapi.in"
INDIANAPI_KEY = (os.environ.get("INDIANAPI_KEY") or "").strip() or None

CACHE_KEY_PREFIX = "indianapi:fundamentals:"
CACHE_TRADING_DAYS = 5

RATE_LIMIT_KEY = "indianapi:last_request_ts"
MIN_REQUEST_INTERVAL_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# group162 (item 3): 429 cooldown + per-symbol failure skip.
#
# At the open VINCOFE, SATIN, KOHINOOR, COMSYN, KKCL and DCI each hit IndianAPI
# again and again and got 429 every time, because nothing here remembered a
# rate-limit answer. A 429 now starts a process-wide cooldown (no request for ANY
# symbol until it ends, and no rate-limit slot is taken); any other failure keeps
# only that symbol out for a while. Cached data (fresh or stale) is still served.
# Settings (blank/invalid values fall back to the defaults):
#   INDIANAPI_COOLDOWN=0          turn the whole thing off (old behaviour)
#   INDIANAPI_COOLDOWN_S          first 429 wait, default 120; doubles per 429 in a row
#   INDIANAPI_COOLDOWN_MAX_S      cap for the wait (and for Retry-After), default 900
#   INDIANAPI_SYMBOL_FAIL_TTL_S   per-symbol skip after a failure, default 600; 0 = off
# ---------------------------------------------------------------------------
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_STREAK = 0
_SYMBOL_FAIL: Dict[str, float] = {}
_SYMBOL_FAIL_MAX = 2000


def _env_pos_float(name: str, default: float, allow_zero: bool = False) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v != v or v in (float("inf"), float("-inf")):  # nan / inf
        return default
    if v < 0 or (v == 0 and not allow_zero):
        return default
    return v


def _backoff_cfg():
    """(enabled, first_wait_s, max_wait_s, symbol_fail_ttl_s)"""
    raw = (os.environ.get("INDIANAPI_COOLDOWN") or "").strip().lower()
    enabled = raw not in ("0", "false", "no", "off")
    return (
        enabled,
        _env_pos_float("INDIANAPI_COOLDOWN_S", 120.0),
        _env_pos_float("INDIANAPI_COOLDOWN_MAX_S", 900.0),
        _env_pos_float("INDIANAPI_SYMBOL_FAIL_TTL_S", 600.0, allow_zero=True),
    )


def reset_backoff() -> None:
    """Clear cooldown and per-symbol failures (used by tests)."""
    global _COOLDOWN_UNTIL, _COOLDOWN_STREAK
    _COOLDOWN_UNTIL = 0.0
    _COOLDOWN_STREAK = 0
    _SYMBOL_FAIL.clear()


def _in_cooldown() -> bool:
    try:
        if not _backoff_cfg()[0]:
            return False
        return time.monotonic() < _COOLDOWN_UNTIL
    except Exception:
        return False


def _symbol_blocked(symbol: str) -> bool:
    try:
        enabled, _f, _m, ttl = _backoff_cfg()
        if not enabled or ttl <= 0:
            return False
        until = _SYMBOL_FAIL.get((symbol or "").upper())
        return until is not None and time.monotonic() < until
    except Exception:
        return False


def _note_rate_limited(response) -> None:
    """Start/extend the process-wide cooldown after a 429."""
    global _COOLDOWN_UNTIL, _COOLDOWN_STREAK
    try:
        enabled, first, cap, _ttl = _backoff_cfg()
        if not enabled:
            return
        wait = min(first * (2 ** min(_COOLDOWN_STREAK, 10)), cap)
        try:
            ra = float((getattr(response, "headers", None) or {}).get("Retry-After", ""))
            if ra == ra and ra > wait:
                wait = ra
        except (TypeError, ValueError):
            pass
        wait = min(wait, cap)
        _COOLDOWN_STREAK += 1
        _COOLDOWN_UNTIL = time.monotonic() + wait
        logger.warning("IndianAPI 429 — pausing all IndianAPI requests for %.0fs (429 #%d in a row)",
                       wait, _COOLDOWN_STREAK)
    except Exception:
        pass


def _note_success() -> None:
    global _COOLDOWN_STREAK
    _COOLDOWN_STREAK = 0


def _note_symbol_failure(symbol: str) -> None:
    try:
        enabled, _f, _m, ttl = _backoff_cfg()
        if not enabled or ttl <= 0:
            return
        now = time.monotonic()
        key = (symbol or "").upper()
        if key not in _SYMBOL_FAIL and len(_SYMBOL_FAIL) >= _SYMBOL_FAIL_MAX:
            for k in [k for k, v in _SYMBOL_FAIL.items() if v <= now]:
                del _SYMBOL_FAIL[k]
            if len(_SYMBOL_FAIL) >= _SYMBOL_FAIL_MAX:
                return
        _SYMBOL_FAIL[key] = now + ttl
    except Exception:
        pass


def _get_redis_client():
    """Unused — storage is kv_cache (memory + Neon). Kept for call-site compat."""
    return None



def _cache_get(redis_client, symbol: str):
    # BUG FIX (2026-09-03, pyflakes audit): the `try: return json.loads(raw)`
    # block below was dead — the `return None` above it makes this function
    # return unconditionally, so the dead code was never reachable — but it
    # referenced `raw`, a name never assigned anywhere in this function, a
    # leftover from an earlier version where _kv.get() returned a raw JSON
    # string that needed decoding. _kv.get() now returns an already-parsed
    # value, so there is nothing left to decode; removed the unreachable
    # (and, if it ever became reachable, broken) block rather than leaving
    # it as confusing dead weight.
    key = CACHE_KEY_PREFIX + symbol.upper()
    if _kv is not None:
        try:
            return _kv.get(key)
        except Exception:
            return None
    return None


def _cache_set(redis_client, symbol: str, payload: Dict[str, Any]) -> None:
    key = CACHE_KEY_PREFIX + symbol.upper()
    if _kv is not None:
        try:
            _kv.set(key, payload, ttl=7 * 86400)
        except Exception as e:
            logger.debug("indianapi cache set: %s", e)



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
    """Shared token-bucket gate (rate_limiter.py, "indianapi" bucket) instead
    of a plain process-local last-timestamp sleep — this way IndianAPI calls
    from fundamentals, the refill-additional job, and weekend hydrator all
    share the same real limit instead of each independently pacing itself
    at 1 req/sec and collectively exceeding it when more than one runs at
    once."""
    global _MEM_LAST_TS
    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("indianapi", weight=1)
        return
    except Exception:
        pass
    # Fallback if rate_limiter.py isn't deployed alongside this service yet
    now = time.time()
    wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _MEM_LAST_TS)
    if wait > 0:
        time.sleep(wait)
    _MEM_LAST_TS = time.time()


def _fetch_from_indianapi(symbol: str) -> Optional[Dict[str, Any]]:
    if not INDIANAPI_KEY:
        logger.warning("INDIANAPI_KEY not set — cannot use IndianAPI fallback for %s", symbol)
        return None
    if _in_cooldown() or _symbol_blocked(symbol):
        logger.debug("IndianAPI skipped for %s (cooldown / recent failure)", symbol)
        return None
    _enforce_rate_limit(None)
    timeout = REQUEST_TIMEOUT_SECONDS
    try:
        from rate_limiter import suggested_timeout as rl_timeout
        timeout = rl_timeout(REQUEST_TIMEOUT_SECONDS, "indianapi")
    except Exception:
        pass
    try:
        response = requests.get(
            f"{INDIANAPI_BASE_URL}/stock",
            params={"name": symbol},
            headers={"x-api-key": INDIANAPI_KEY},
            timeout=timeout,
        )
        if getattr(response, "status_code", None) == 429:
            _note_rate_limited(response)
            return None
        response.raise_for_status()
        data = response.json()
        _note_success()
        return data
    except requests.RequestException as e:
        err_resp = getattr(e, "response", None)
        if getattr(err_resp, "status_code", None) == 429:
            _note_rate_limited(err_resp)
            return None
        logger.error("IndianAPI request failed for %s: %s", symbol, e)
        _note_symbol_failure(symbol)
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
