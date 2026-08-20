import math
"""
Market Data Service
--------------------
Single responsibility: fetch raw market data (price history, quote, company info)
for Indian equities from free public sources (yfinance) and serve over REST.
All data is cached with TTL that depends on market hours:
- During NSE trading hours (09:15-15:30 IST, Mon-Fri): TTL = 300 seconds (5 min)
- Outside: TTL = 21600 seconds (6 hours)

v2.2 – reduced retries and timeouts for faster responses.
"""
import os
import time
import json
import logging
import math
import random
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional

import requests
import yfinance as yf
from upstash_redis import Redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import httpx
from circuit_breaker import get_breaker, all_snapshots
import gc
MAX_HISTORY_ROWS = int(os.environ.get('MAX_HISTORY_ROWS', '260'))  # ~1y daily, 512MB-safe
MAX_HISTORY_PERIOD = os.environ.get('MAX_HISTORY_PERIOD', '1y')

def _report_rate_limit(status: int, path: str = "", detail: str = "", symbol: str = "") -> None:
    """
    Dual-write rate-limit events:
      1) Direct Neon KV so the gateway Rate Limit Dashboard is not blind
      2) Best-effort POST to gateway /ops/rate-limits/event for in-process monitor
    """
    try:
        from circuit_breaker import record_rate_limit_hit
        record_rate_limit_hit(
            provider="market_data",
            status=int(status),
            path=path or "",
            detail=str(detail)[:200],
            symbol=symbol or "",
        )
    except Exception:
        pass
    try:
        gw = os.environ.get("API_GATEWAY_URL", "").rstrip("/")
        if not gw:
            return
        requests.post(
            f"{gw}/ops/rate-limits/event",
            json={
                "source": "market_data",
                "status": status,
                "path": path,
                "detail": str(detail)[:200],
                "symbol": symbol,
            },
            timeout=2,
        )
    except Exception:
        pass


# ── yfinance session patch ────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})

try:
    yf.set_session(session)
except AttributeError:
    try:
        yf.shared._session = session
    except AttributeError:
        pass

try:
    yf.set_tz_cache_location("/tmp/yfinance_tz")
except AttributeError:
    pass

# ── Helpers ────────────────────────────────────────────────────────────────────
def _normalize_de_ratio(val, sector=None):
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    sec = (str(sector or "")).lower()
    is_fin = any(x in sec for x in ("bank", "financial", "insurance"))
    if v > 50 and not is_fin:
        v = v / 100.0
    elif v > 200 and is_fin:
        v = v / 100.0
    return round(v, 2)


def _safe(val, decimals=2):
    try:
        f = float(val)
        if math.isnan(f) or not math.isfinite(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None

def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def _compute_growth(current, previous):
    if previous is None or previous == 0:
        return None
    try:
        g = ((float(current) - float(previous)) / float(previous)) * 100
        if g != g or abs(g) == float("inf"):  # NaN/Inf
            return None
        return g
    except Exception:
        return None

# Global yfinance concurrency guard (free-tier / Yahoo rate-limit safe).
# Caps concurrent Yahoo calls across quote + history + fundamentals so a
# parallel market scan does not stampede Yahoo and get empty responses.
import threading
_YFINANCE_MAX_CONCURRENT = int(os.getenv("YFINANCE_MAX_CONCURRENT", "1"))
_yf_semaphore = threading.Semaphore(_YFINANCE_MAX_CONCURRENT)
_YF_MIN_INTERVAL = float(os.getenv("YFINANCE_MIN_INTERVAL_SEC", "0.35"))
_yf_last_call = 0.0
_yf_lock = threading.Lock()

def _yf_rate_limited(func):
    """Run func under the yfinance semaphore + small spacing between calls."""
    global _yf_last_call
    with _yf_semaphore:
        with _yf_lock:
            now = time.time()
            gap = _YF_MIN_INTERVAL - (now - _yf_last_call)
            if gap > 0:
                time.sleep(gap)
            _yf_last_call = time.time()
        return func()

def _with_retry(func, max_retries=4, base_delay=1.0):
    """Retry with exponential backoff (Yahoo rate limits / free-tier).

    Circuit breaker opens after repeated Yahoo failures so the rest of the
    scan fails fast instead of stacking 70s timeouts.
    """
    br = get_breaker("yfinance", failure_threshold=6, recovery_timeout=120)
    if not br.allow():
        raise RuntimeError(f"yfinance circuit open; retry in {br.retry_after():.0f}s")
    last_err = None
    for attempt in range(max_retries):
        try:
            result = _yf_rate_limited(func)
            br.record_success()
            return result
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "too many" in msg or "rate limit" in msg:
                _set_cooldown("yfinance", _YF_COOLDOWN_SEC)
                br.record_failure(str(e))
                _report_rate_limit(429, path="yfinance", detail=str(e)[:200])
                raise
            if attempt == max_retries - 1:
                br.record_failure(str(e))
                raise
            wait = base_delay * (2 ** attempt)
            time.sleep(wait)
    if last_err:
        br.record_failure(str(last_err))
        raise last_err


def is_market_open() -> bool:
    """Return True if current time is within NSE trading hours (Mon-Fri, 09:15-15:30 IST)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def _sanitize_for_json(obj):
    """Replace NaN/Inf so FastAPI json.dumps never 500s (fundamentals etc.)."""
    import math
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    try:
        import numpy as np
        if isinstance(obj, (np.floating,)):
            f = float(obj)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, np.ndarray):
            return [_sanitize_for_json(x) for x in obj.tolist()]
    except Exception:
        pass
    return obj


def get_cache_ttl() -> int:
    """Return TTL in seconds: 300 if market open, else 21600 (6 hours)."""
    return 300 if is_market_open() else 21600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-data-service")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

app = FastAPI(title="Stockky Market Data Service", version="2.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"}
    )

# ── Cache: memory-first (unlimited). Redis only if USE_REDIS=1 ─────────────────
USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")  # default OFF — Neon/memory only
# Kill-switches — any of these force Redis completely off (even if credentials exist)
if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False
if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False
# Hard safety: if not explicitly USE_REDIS=1, never touch Upstash (credentials alone are not enough)
if os.getenv("USE_REDIS", "0").strip() in ("", "0", "false", "False", "no", "NO"):
    USE_REDIS = False


FALLBACK_TTL_SECONDS = 30 * 24 * 60 * 60

# Global Yahoo/upstream cooldown after 429 (stops stampede + Redis write storm)
_YF_COOLDOWN_UNTIL = 0.0
_YF_COOLDOWN_SEC = float(os.getenv("YFINANCE_COOLDOWN_SEC", "180"))  # 3 min
_UPSTREAM_COOLDOWN = {}  # name -> until epoch

def _in_cooldown(name: str = "yfinance") -> bool:
    return time.time() < _UPSTREAM_COOLDOWN.get(name, 0)

def _set_cooldown(name: str = "yfinance", sec: float = None):
    global _YF_COOLDOWN_UNTIL
    until = time.time() + (sec if sec is not None else _YF_COOLDOWN_SEC)
    _UPSTREAM_COOLDOWN[name] = until
    if name == "yfinance":
        _YF_COOLDOWN_UNTIL = until
    logger.warning("%s cooldown until +%.0fs (rate limit / 429)", name, sec or _YF_COOLDOWN_SEC)


class _MemCache:
    def __init__(self, max_keys: int = 6000):
        self._d = {}
        self._lock = threading.Lock()
        self._max = max_keys

    def get(self, key: str):
        with self._lock:
            e = self._d.get(key)
            if not e:
                return None
            val, exp = e
            if exp is not None and time.time() > exp:
                self._d.pop(key, None)
                return None
            return val

    def set(self, key: str, value, ttl: int = None):
        with self._lock:
            if len(self._d) >= self._max and key not in self._d:
                # drop expired + some oldest keys
                now = time.time()
                for k in list(self._d.keys())[: max(50, self._max // 20)]:
                    v = self._d.get(k)
                    if not v or (v[1] is not None and v[1] < now):
                        self._d.pop(k, None)
                if len(self._d) >= self._max:
                    for k in list(self._d.keys())[:50]:
                        self._d.pop(k, None)
            exp = (time.time() + ttl) if ttl else None
            self._d[key] = (value, exp)

    def ttl(self, key: str) -> int:
        with self._lock:
            e = self._d.get(key)
            if not e:
                return -2
            _, exp = e
            if exp is None:
                return -1
            left = int(exp - time.time())
            return left if left > 0 else -2


_mem = _MemCache()
cache = None  # optional Upstash
try:
    if USE_REDIS and UPSTASH_URL and UPSTASH_TOKEN:
        cache = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
        cache.ping()
        logger.info("Connected to Upstash Redis (USE_REDIS=1)")
    else:
        cache = None  # hard-disable even if credentials present
        logger.info("Market-data cache: in-memory only (USE_REDIS=0) — no Upstash commands")
except Exception as e:
    logger.warning("Redis unavailable (%s). Memory-only cache.", e)
    cache = None


def _cache_ttl(key: str) -> int:
    t = _mem.ttl(key)
    if t != -2:
        return t
    if not cache:
        return -2
    try:
        tt = cache.ttl(key)
        return int(tt) if tt is not None else -2
    except Exception:
        return -2


def _should_soft_refresh(key: str, soft_window: int = 30) -> bool:
    """Soft refresh only when NOT rate-limited (avoids stampede during 429)."""
    if _in_cooldown("yfinance") or _in_cooldown("nse"):
        return False
    t = _cache_ttl(key)
    return 0 < t <= soft_window


def _cache_get(key: str):
    val = _mem.get(key)
    if val is not None:
        return val
    if not cache:
        return None
    try:
        raw = cache.get(key)
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        _mem.set(key, parsed, ttl=60)  # warm memory
        return parsed
    except Exception:
        return None


def _cache_set(key: str, value: dict, ttl: int = None):
    if ttl is None:
        ttl = get_cache_ttl()
    value = _sanitize_for_json(value)
    _mem.set(key, value, ttl=ttl)
    # HARD OFF: never write Upstash unless USE_REDIS explicitly enabled
    if not USE_REDIS or not cache:
        return
    try:
        cache.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.debug("redis set failed: %s", e)


def _fallback_get(key: str):
    val = _mem.get(f"fallback:{key}")
    if val is not None:
        return val
    if not cache:
        return None
    try:
        raw = cache.get(f"fallback:{key}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _fallback_set(key: str, value: dict):
    value = _sanitize_for_json(value)
    _mem.set(f"fallback:{key}", value, ttl=FALLBACK_TTL_SECONDS)
    if not USE_REDIS or not cache:
        return
    try:
        cache.setex(f"fallback:{key}", FALLBACK_TTL_SECONDS, json.dumps(value, default=str))
    except Exception:
        pass

# Yahoo Finance tickers for common NSE index display names.
# Without this, normalize_symbol("NIFTY NEXT 50") → "NIFTY NEXT 50.NS" → 404/503.
INDEX_YAHOO_MAP = {
    "NIFTY": "^NSEI",
    "NIFTY 50": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY NEXT 50": "^NSMIDCP",  # proxy; Yahoo ^NN50 sometimes unavailable
    "NIFTYNEXT50": "^NSMIDCP",
    "NIFTY NEXT50": "^NSMIDCP",
    "NIFTY BANK": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "BANK NIFTY": "^NSEBANK",
    "BANKNIFTY": "^NSEBANK",
    "NIFTY MIDCAP 100": "^NSEMDCP100",
    "NIFTY MIDCAP100": "^NSEMDCP100",
    "NIFTYMIDCAP100": "^NSEMDCP100",
    "NIFTY MIDCAP 150": "NIFTY_MIDCAP_150.NS",
    "NIFTY MIDCAP150": "NIFTY_MIDCAP_150.NS",
    "NIFTYMIDCAP150": "NIFTY_MIDCAP_150.NS",
    "NIFTY SMALLCAP 100": "NIFTYSMLCAP100.NS",
    "NIFTY SMALLCAP 250": "NIFTYSMLCAP250.NS",
    "NIFTY IT": "^CNXIT",
    "NIFTYIT": "^CNXIT",
    "SENSEX": "^BSESN",
    "BSE SENSEX": "^BSESN",
    "INDIA VIX": "^INDIAVIX",
    "INDIAVIX": "^INDIAVIX",
}
# Alternate Yahoo symbols to try if primary fails
INDEX_YAHOO_FALLBACKS = {
    "^NSMIDCP": ["^NSEI"],
    "NIFTY_MIDCAP_150.NS": ["^NSEMDCP100", "^NSEI"],
    "^NSEMDCP100": ["^NSEI"],
}


def normalize_symbol(symbol: str) -> str:
    """Map equity and index symbols to Yahoo-compatible tickers.

    Equities get .NS if missing. Index *names* (with spaces) map to ^… tickers
    so history/quote never request "NIFTY NEXT 50.NS".
    """
    raw = (symbol or "").strip()
    if not raw:
        return raw
    upper = raw.upper().replace("_", " ")
    # Already a Yahoo index
    if raw.startswith("^"):
        return raw
    # Direct map (exact)
    key = upper.replace("  ", " ").strip()
    if key in INDEX_YAHOO_MAP:
        return INDEX_YAHOO_MAP[key]
    # Compact key without spaces
    compact = key.replace(" ", "")
    for k, v in INDEX_YAHOO_MAP.items():
        if k.replace(" ", "") == compact:
            return v
    # Equity path
    sym = raw.strip().upper()
    # Do not append .NS to names that still look like multi-word indices
    if " " in sym or sym.startswith("NIFTY") and not sym.endswith(".NS"):
        # Unknown nifty-like — try as-is with underscores for Yahoo
        if "NIFTY" in sym:
            return sym.replace(" ", "_") + ("" if sym.endswith(".NS") else ".NS")
    if not sym.endswith(".NS") and not sym.endswith(".BO"):
        sym = f"{sym}.NS"
    return sym


class QuoteResponse(BaseModel):
    symbol: str
    name: Optional[str]
    price: Optional[float]
    previous_close: Optional[float]
    day_change_pct: Optional[float]
    day_high: Optional[float]
    day_low: Optional[float]
    volume: Optional[int]
    market_cap: Optional[float]
    pe_ratio: Optional[float]
    fetched_at: str

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "Stockky Market Data Service",
        "version": "2.2.0",
        "status": "running",
        "cache_enabled": bool(cache),
        "endpoints": {
            "/health": "GET – health check",
            "/quote/{symbol}": "GET – latest quote",
            "/history/{symbol}": "GET – OHLCV candles",
            "/fundamentals/{symbol}": "GET – raw fundamental data",
        },
    }

@app.get("/health")
def health(warm: bool = Query(False, description="If true, touch yfinance once to reduce cold latency")):
    # Lightweight – returns quickly; optional warm for free-tier wake
    if warm:
        try:
            def _touch():
                t = yf.Ticker("^NSEI")
                t.history(period="5d", interval="1d")
            _with_retry(_touch, max_retries=2, base_delay=0.5)
            return {"status": "ok", "service": "market-data-service", "cache": bool(cache), "warmed": True, "circuits": all_snapshots()}
        except Exception as e:
            return {"status": "ok", "service": "market-data-service", "cache": bool(cache), "warmed": False, "warm_error": str(e)[:120]}
    return {"status": "ok", "service": "market-data-service", "cache": bool(cache), "circuits": all_snapshots()}

@app.get("/wake")
def wake():
    """Explicit cold-start wake used by api-gateway before scans."""
    return health(warm=True)

# ── NSE India Official API (Primary) ─────────────────────────────────────────
_nse_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "DNT": "1",
}

def _fetch_nse_quote(symbol: str) -> Optional[dict]:
    try:
        clean_sym = symbol.replace(".NS", "").replace(".BO", "")
        with httpx.Client(headers=_nse_headers, timeout=10) as client:
            client.get("https://www.nseindia.com")
            time.sleep(0.3)
            url = f"https://www.nseindia.com/api/quote-equity?symbol={clean_sym}"
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data and "priceInfo" in data:
                    return data
    except Exception as e:
        logger.warning(f"NSE Quote fetch failed: {e}")
    return None

def _fetch_nse_fundamentals(symbol: str) -> Optional[dict]:
    try:
        clean_sym = symbol.replace(".NS", "").replace(".BO", "")
        with httpx.Client(headers=_nse_headers, timeout=10) as client:
            client.get("https://www.nseindia.com")
            url = f"https://www.nseindia.com/api/quote-equity?symbol={clean_sym}&section=secinfo"
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data and "secInfo" in data:
                    return data
    except Exception as e:
        logger.warning(f"NSE Fundamentals fetch failed: {e}")
    return None

def _fetch_price_from_yahoo_raw(symbol: str) -> Optional[float]:
    try:
        for sym in [symbol, symbol.replace(".NS", "")]:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                chart = data.get("chart", {})
                result = chart.get("result", [])
                if result and "meta" in result[0]:
                    price = result[0]["meta"].get("regularMarketPrice")
                    if price is not None:
                        logger.info(f"Yahoo Raw API fallback found price for {sym}: {price}")
                        return price
    except Exception as e:
        logger.warning(f"Yahoo Raw API fallback failed: {e}")
    return None


def _waterfall_yahoo_history_price(symbol: str) -> Optional[float]:
    """Primary free path: yfinance 1d Close for NSE (.NS) / BSE (.BO)."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None
    for suffix in (".NS", ".BO", ""):
        ticker = f"{base}{suffix}" if suffix else base
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                px = float(hist["Close"].dropna().iloc[-1])
                if px > 0:
                    return px
        except Exception as e:
            logger.debug("yahoo history %s: %s", ticker, e)
    return None


def _waterfall_twelvedata_price(symbol: str) -> Optional[float]:
    key = TWELVE_DATA_API_KEY
    if not key:
        return None
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    # Indian symbols often need exchange suffix on TwelveData
    candidates = [f"{base}.NSE", f"{base}.BSE", base]
    for sym in candidates:
        try:
            url = f"https://api.twelvedata.com/price?symbol={sym}&apikey={key}"
            resp = httpx.get(url, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json() if isinstance(resp.json(), dict) else {}
                px = _safe(data.get("price"))
                if px is not None and px > 0:
                    logger.info("TwelveData waterfall hit %s → ₹%.2f", sym, px)
                    return float(px)
        except Exception as e:
            logger.debug("TwelveData %s: %s", sym, e)
    return None


def _waterfall_polygon_price(symbol: str) -> Optional[float]:
    key = POLYGON_API_KEY
    if not key:
        return None
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    # Polygon primarily US; still try as last resort with .NS ticker style
    for sym in (f"X:{base}", base, f"{base}.NS"):
        try:
            url = f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={key}"
            resp = httpx.get(url, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json() if isinstance(resp.json(), dict) else {}
                results = data.get("results") or []
                if results:
                    px = _safe(results[0].get("c"))
                    if px is not None and px > 0:
                        logger.info("Polygon waterfall hit %s → ₹%.2f", sym, px)
                        return float(px)
        except Exception as e:
            logger.debug("Polygon %s: %s", sym, e)
    return None

@app.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    sym = normalize_symbol(symbol)
    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)
    # Soft refresh only if not rate-limited; prefer stale price over N/A during 429
    if cached and _should_soft_refresh(cache_key, soft_window=45):
        logger.info("Soft-TTL refresh for %s", cache_key)
        # keep cached as safety net if all upstreams fail
        soft_cached = cached
        cached = None
    else:
        soft_cached = cached
    if cached:
        return cached
    if soft_cached and (_in_cooldown("yfinance") or _in_cooldown("nse")):
        return soft_cached

    # 0. WATERFALL PRIMARY: Yahoo 1d history (bypasses NSE 403 on Render)
    price = None
    source = None
    if not _in_cooldown("yfinance"):
        try:
            ypx = _waterfall_yahoo_history_price(sym)
            if ypx is not None and ypx > 0:
                price = ypx
                source = "yahoo"
                logger.info("Yahoo waterfall primary hit %s → ₹%.2f", sym, ypx)
        except Exception as e:
            logger.debug("yahoo primary %s: %s", sym, e)

    # 1. NSE India (skip if NSE cooldown) — often 403 on cloud IPs
    if price is None:
      if _in_cooldown("nse"):
          nse_data = None
      else:
          nse_data = _fetch_nse_quote(sym)
    else:
        nse_data = None
    if nse_data:
        price = _safe(nse_data.get("priceInfo", {}).get("lastPrice"))
        if price is not None:
            result = {
                "symbol": sym,
                "name": nse_data.get("securityInfo", {}).get("symbol") or sym,
                "price": price,
                "previous_close": _safe(nse_data.get("priceInfo", {}).get("previousClose")),
                "day_change_pct": _safe(nse_data.get("priceInfo", {}).get("pChange")),
                "day_high": _safe(nse_data.get("priceInfo", {}).get("dayHigh")),
                "day_low": _safe(nse_data.get("priceInfo", {}).get("dayLow")),
                "volume": _safe_int(nse_data.get("priceInfo", {}).get("totalTradedVolume")),
                "market_cap": _safe(nse_data.get("priceInfo", {}).get("marketCap")),
                "pe_ratio": _safe(nse_data.get("priceInfo", {}).get("pe")),
                "source": "nse",
                "fetched_at": datetime.utcnow().isoformat(),
            }
            result = _sanitize_for_json(result)
            _cache_set(cache_key, result)
            return result

    # Early return if Yahoo primary already resolved price
    if price is not None and price > 0:
        result = {
            "symbol": sym,
            "name": sym,
            "price": float(price),
            "cmp": float(price),
            "source": source or "yahoo",
            "fetched_at": datetime.utcnow().isoformat(),
        }
        result = _sanitize_for_json(result)
        _cache_set(cache_key, result)
        return result

    # 2. Alpha Vantage
    price = None
    if ALPHA_VANTAGE_API_KEY:
        possible_symbols = [sym, sym.replace(".NS", "")]
        for alpha_sym in possible_symbols:
            try:
                alpha_url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={alpha_sym}&apikey={ALPHA_VANTAGE_API_KEY}"
                alpha_resp = httpx.get(alpha_url, timeout=8)
                if alpha_resp.status_code == 200:
                    alpha_data = alpha_resp.json()
                    quote = alpha_data.get("Global Quote", {})
                    if quote:
                        price = _safe(quote.get("05. price"))
                        if price is not None:
                            logger.info(f"Alpha Vantage fallback found price for {alpha_sym}: {price}")
                            break
            except Exception as e:
                logger.warning(f"Alpha Vantage fallback for {alpha_sym} failed: {e}")

    # 3. Twelve Data
    if price is None and TWELVE_DATA_API_KEY:
        try:
            clean_sym = sym.replace(".NS", "").replace(".BO", "")
            url = f"https://api.twelvedata.com/price?symbol={clean_sym}&apikey={TWELVE_DATA_API_KEY}"
            resp = httpx.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                price = _safe(data.get("price"))
                if price is not None:
                    logger.info(f"Twelve Data fallback found price for {sym}: {price}")
        except Exception as e:
            logger.warning(f"Twelve Data fallback failed: {e}")

    # 4. Polygon.io
    if price is None and POLYGON_API_KEY:
        try:
            clean_sym = sym.replace(".NS", "").replace(".BO", "")
            url = f"https://api.polygon.io/v1/open-close/{clean_sym}/latest?apiKey={POLYGON_API_KEY}"
            resp = httpx.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                price = _safe(data.get("close"))
                if price is not None:
                    logger.info(f"Polygon.io fallback found price for {sym}: {price}")
        except Exception as e:
            logger.warning(f"Polygon.io fallback failed: {e}")

    # 4b. TwelveData / Polygon structured waterfall (if earlier steps used different endpoints)
    if price is None:
        price = _waterfall_twelvedata_price(sym)
        if price is not None:
            source = "twelvedata"
    if price is None:
        price = _waterfall_polygon_price(sym)
        if price is not None:
            source = "polygon"

    # 5. Yahoo Raw API
    if price is None:
        price = _fetch_price_from_yahoo_raw(sym)
        if price is not None:
            source = source or "yahoo_raw"

    # 6. yfinance final fallback
    if price is None:
        try:
            ticker = yf.Ticker(sym)
            ticker._tz = "Asia/Kolkata"
            info = _with_retry(lambda: ticker.info, max_retries=2, base_delay=0.5)
            if info:
                price = info.get("regularMarketPrice") or info.get("last_price")
                prev_close = info.get("previousClose")
                change_pct = None
                if price and prev_close:
                    change_pct = round(((price - prev_close) / prev_close) * 100, 2)

                result = {
                    "symbol": sym,
                    "name": info.get("longName") or info.get("shortName") or sym,
                    "price": price,
                    "previous_close": prev_close,
                    "day_change_pct": change_pct,
                    "day_high": info.get("dayHigh"),
                    "day_low": info.get("dayLow"),
                    "volume": info.get("volume"),
                    "market_cap": info.get("marketCap"),
                    "pe_ratio": info.get("trailingPE"),
                    "fetched_at": datetime.utcnow().isoformat(),
                }
                _cache_set(cache_key, result)
                return result
        except Exception as e:
            logger.warning(f"yfinance quote failed for {sym}: {e}")

    # If we have a price from one of the APIs, build minimal response
    if price is not None:
        result = {
            "symbol": sym,
            "name": sym,
            "price": price,
            "previous_close": None,
            "day_change_pct": None,
            "day_high": None,
            "day_low": None,
            "volume": None,
            "market_cap": None,
            "pe_ratio": None,
            "fetched_at": datetime.utcnow().isoformat(),
        }
        result = _sanitize_for_json(result)
        _cache_set(cache_key, result)
        _fallback_set(cache_key, result)
        return result

    # No price – return fallback
    logger.warning("Could not fetch price for %s from any source. Returning fallback.", sym)
    # Prefer last good cached/soft value over empty N/A
    if soft_cached and isinstance(soft_cached, dict) and soft_cached.get("price") is not None:
        return soft_cached
    fb = _fallback_get(cache_key)
    if fb and isinstance(fb, dict) and fb.get("price") is not None:
        return fb
    result = {
        "symbol": sym,
        "name": sym,
        "price": None,
        "previous_close": None,
        "day_change_pct": None,
        "day_high": None,
        "day_low": None,
        "volume": None,
        "market_cap": None,
        "pe_ratio": None,
        "fetched_at": datetime.utcnow().isoformat(),
    }
    _cache_set(cache_key, result)
    return result

@app.get("/history/{symbol}")
def get_history(
    symbol: str,
    period: str = Query("6mo", description="1mo, 3mo, 6mo, 1y, 2y, 5y"),
    interval: str = Query("1d", description="1d, 1wk, 1h"),
):
    # Cap long periods on free-tier 512MB dynos
    _period_rank = {"1mo": 1, "3mo": 2, "6mo": 3, "1y": 4, "2y": 5, "5y": 6}
    if _period_rank.get(period, 4) > _period_rank.get(MAX_HISTORY_PERIOD, 4):
        period = MAX_HISTORY_PERIOD
    """OHLCV for equities and indices.

    Index display names (e.g. "NIFTY NEXT 50") are mapped via normalize_symbol
    to Yahoo tickers. If the primary ticker fails, INDEX_YAHOO_FALLBACKS are tried
    so prediction/sentiment never get stuck on 404/503 for known NSE indices.
    """
    sym = normalize_symbol(symbol)
    candidates = [sym]
    for _fb in INDEX_YAHOO_FALLBACKS.get(sym, []):
        if _fb not in candidates:
            candidates.append(_fb)
    # Also try ^NSEI as last resort for any NIFTY* request
    raw_u = (symbol or "").upper()
    if "NIFTY" in raw_u and "^NSEI" not in candidates:
        candidates.append("^NSEI")

    cache_key = f"history:{sym}:{period}:{interval}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    last_err = None
    for cand in candidates:
        try:
            ticker = yf.Ticker(cand)
            try:
                ticker._tz = "Asia/Kolkata"
            except Exception:
                pass
            df = _with_retry(
                lambda t=ticker: t.history(period=period, interval=interval, auto_adjust=True),
                max_retries=3,
                base_delay=0.8,
            )
            if df is None or df.empty:
                last_err = f"empty history for {cand}"
                logger.info("No history for candidate %s (requested %s)", cand, symbol)
                continue

            candles = []
            for idx, row in df.iterrows():
                if _safe(row["Close"]) is None:
                    continue
                try:
                    vol = int(row["Volume"]) if math.isfinite(float(row["Volume"])) else 0
                except Exception:
                    vol = 0
                candles.append({
                    "date": idx.strftime("%Y-%m-%d %H:%M"),
                    "open": _safe(row["Open"]),
                    "high": _safe(row["High"]),
                    "low": _safe(row["Low"]),
                    "close": _safe(row["Close"]),
                    "volume": vol,
                })

            if not candles:
                last_err = f"no valid candles for {cand}"
                del df
                gc.collect()
                continue

            if len(candles) > MAX_HISTORY_ROWS:
                candles = candles[-MAX_HISTORY_ROWS:]
            del df
            gc.collect()

            result = {
                "symbol": cand,
                "requested": (symbol or "").strip(),
                "period": period,
                "interval": interval,
                "candles": candles,
            }
            hist_ttl = 900 if is_market_open() else 21600
            _cache_set(cache_key, result, ttl=hist_ttl)
            if cand != sym:
                logger.info("History for %s served via fallback ticker %s", symbol, cand)
            return result
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            logger.warning("History candidate %s failed for %s: %s", cand, symbol, e)
            continue

    # All candidates failed
    detail = last_err or f"No history for {symbol}"
    # 404 for unknown symbols; 503 only when Yahoo looks transient
    if last_err and any(x in last_err.lower() for x in ("timeout", "429", "503", "connection", "temporarily")):
        _report_rate_limit(503, path=f"/history/{symbol}", detail=detail, symbol=symbol)
        raise HTTPException(status_code=503, detail=f"Temporary history unavailable for {symbol}: {detail}")
    raise HTTPException(status_code=404, detail=f"No history found for {symbol}: {detail}")

@app.get("/fundamentals/{symbol}")
def get_fundamentals_raw(symbol: str):
    try:
        return _get_fundamentals_inner(symbol)
    except Exception as e:
        logger.warning("fundamentals failed for %s: %s", symbol, e)
        return _sanitize_for_json({
            "symbol": str(symbol).upper().replace(".NS", "") + ".NS" if not str(symbol).endswith(".NS") else str(symbol),
            "error": "fundamentals_unavailable",
            "message": str(e)[:200],
            "pe_ratio": None,
            "roe": None,
        })

def _get_fundamentals_inner(symbol: str):
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return _sanitize_for_json(cached)
    if _in_cooldown("yfinance"):
        fb = _fallback_get(cache_key)
        if fb:
            return _sanitize_for_json(fb)
        return _sanitize_for_json({
            "symbol": sym,
            "error": "yfinance_cooldown",
            "message": "Yahoo rate-limited — serving empty fundamentals until cooldown ends",
            "pe_ratio": None,
            "roe": None,
            "revenue_growth": None,
        })

    result = {}
    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"

        info = {}
        try:
            info = _with_retry(lambda: ticker.info, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch info for {sym}: {e}")

        if info is None:
            info = {}

        financials = None
        balance = None
        cashflow = None
        try:
            financials = _with_retry(lambda: ticker.financials, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch financials for {sym}: {e}")
        try:
            balance = _with_retry(lambda: ticker.balance_sheet, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch balance sheet for {sym}: {e}")
        try:
            cashflow = _with_retry(lambda: ticker.cashflow, max_retries=2, base_delay=0.5)
        except Exception as e:
            logger.warning(f"Could not fetch cashflow for {sym}: {e}")

        def _safe_info(key):
            val = info.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        financials_available = financials is not None and not financials.empty
        balance_available = balance is not None and not balance.empty
        cashflow_available = cashflow is not None and not cashflow.empty

        revenue_growth = None
        if financials_available and "Total Revenue" in financials.index:
            rev_series = financials.loc["Total Revenue"]
            if len(rev_series) >= 2:
                current_rev = rev_series.iloc[0]
                prev_rev = rev_series.iloc[1]
                revenue_growth = _compute_growth(current_rev, prev_rev)

        earnings_growth = None
        if financials_available and "Net Income" in financials.index:
            earnings_series = financials.loc["Net Income"]
            if len(earnings_series) >= 2:
                current_earn = earnings_series.iloc[0]
                prev_earn = earnings_series.iloc[1]
                earnings_growth = _compute_growth(current_earn, prev_earn)

        roe = None
        if "returnOnEquity" in info:
            roe = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("returnOnEquity"))) if _safe_info("returnOnEquity") else None
        elif balance_available and "Total Equity Gross Minority Interest" in balance.index:
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if financials_available and "Net Income" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                if equity != 0:
                    roe = (net_income / equity) * 100

        debt_to_equity = None
        if "debtToEquity" in info:
            debt_to_equity = _normalize_de_ratio(_safe_info("debtToEquity"), info.get("sector"))
        elif balance_available and "Total Debt" in balance.index and "Total Equity Gross Minority Interest" in balance.index:
            total_debt = balance.loc["Total Debt"].iloc[0]
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if equity != 0:
                debt_to_equity = total_debt / equity

        free_cashflow = None
        if "freeCashflow" in info:
            free_cashflow = _safe_info("freeCashflow")
        elif cashflow_available and "Free Cash Flow" in cashflow.index:
            free_cashflow = cashflow.loc["Free Cash Flow"].iloc[0]

        profit_margins = None
        if "profitMargins" in info:
            profit_margins = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("profitMargins")))
        else:
            if financials_available and "Net Income" in financials.index and "Total Revenue" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                revenue = financials.loc["Total Revenue"].iloc[0]
                if revenue != 0:
                    profit_margins = (net_income / revenue) * 100

        held_percent_institutions = None
        if "heldPercentInstitutions" in info:
            held_percent_institutions = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("heldPercentInstitutions")))
        elif "institutionalPercent" in info:
            held_percent_institutions = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("institutionalPercent")))

        pe_ratio = _safe_info("trailingPE") if "trailingPE" in info else _safe_info("peRatio")
        forward_pe = _safe_info("forwardPE")
        eps = _safe_info("trailingEps") or _safe_info("eps")
        market_cap = _safe_info("marketCap")
        dividend_yield = None
        if "dividendYield" in info:
            dividend_yield = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("dividendYield")))
        else:
            try:
                divs = ticker.dividends
                if divs is not None and not divs.empty:
                    last_price = _safe_info("regularMarketPrice") or _safe_info("last_price")
                    if last_price:
                        annual_div = float(divs.tail(4).sum())
                        dividend_yield = round(annual_div / last_price * 100, 2)
            except Exception:
                pass

        year_high = _safe_info("fiftyTwoWeekHigh")
        year_low = _safe_info("fiftyTwoWeekLow")
        fifty_day_average = _safe_info("fiftyDayAverage")
        two_hundred_day_average = _safe_info("twoHundredDayAverage")
        year_change_pct = _safe_info("52WeekChange")
        if year_change_pct is not None:
            year_change_pct = year_change_pct * 100

        sector = info.get("sector")
        industry = info.get("industry")

        pe_growth = pe_ratio / earnings_growth if (pe_ratio is not None and earnings_growth is not None and earnings_growth != 0) else None
        ev_ebitda = _safe_info("enterpriseToEbitda")
        price_to_book = _safe_info("priceToBook")
        roce = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("returnOnCapitalEmployed"))) if _safe_info("returnOnCapitalEmployed") else None
        opm = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("operatingMargins")))
        if opm is None and financials_available and "Operating Income" in financials.index and "Total Revenue" in financials.index:
            op_income = financials.loc["Operating Income"].iloc[0]
            revenue = financials.loc["Total Revenue"].iloc[0]
            opm = (op_income / revenue) * 100 if revenue != 0 else None

        current_ratio = None
        if balance_available and "Total Current Assets" in balance.index and "Total Current Liabilities" in balance.index:
            ca = balance.loc["Total Current Assets"].iloc[0]
            cl = balance.loc["Total Current Liabilities"].iloc[0]
            current_ratio = ca / cl if cl != 0 else None

        interest_coverage = None
        if financials_available:
            if "EBIT" in financials.index and "Interest Expense" in financials.index:
                ebit = financials.loc["EBIT"].iloc[0]
                interest = financials.loc["Interest Expense"].iloc[0]
                interest_coverage = ebit / interest if interest != 0 else None
            elif "Operating Income" in financials.index and "Interest Expense" in financials.index:
                ebit = financials.loc["Operating Income"].iloc[0]
                interest = financials.loc["Interest Expense"].iloc[0]
                interest_coverage = ebit / interest if interest != 0 else None

        promoter_holding = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("promoterHolding"))) if "promoterHolding" in info else None
        promoter_pledging = ((lambda v: (v * 100) if isinstance(v, (int, float)) else None)(_safe_info("promoterPledging"))) if "promoterPledging" in info else None

        result = {
            "symbol": sym,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "market_cap": market_cap,
            "dividend_yield": dividend_yield,
            "year_change_pct": year_change_pct,
            "year_high": year_high,
            "year_low": year_low,
            "fifty_day_average": fifty_day_average,
            "two_hundred_day_average": two_hundred_day_average,
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "eps": eps,
            "roe": roe,
            "roce": roce,
            "debt_to_equity": debt_to_equity,
            "free_cashflow": free_cashflow,
            "profit_margins": profit_margins,
            "opm": opm,
            "current_ratio": current_ratio,
            "interest_coverage": interest_coverage,
            "held_percent_insiders": (lambda v: (v * 100) if v is not None else None)(_safe_info("heldPercentInsiders")),
            "held_percent_institutions": held_percent_institutions,
            "price_to_book": price_to_book,
            "pe_growth": pe_growth,
            "ev_ebitda": ev_ebitda,
            "promoter_holding": promoter_holding,
            "promoter_pledging": promoter_pledging,
            "sector": sector,
            "industry": industry,
        }

        logger.info(f"Fundamentals for {sym}: PE={pe_ratio}, ROE={roe}, Revenue growth={revenue_growth}")

    except Exception as e:
        logger.warning(f"YFinance failed for {sym}, falling back to NSE India: {e}")
        nse_data = _fetch_nse_fundamentals(sym)
        if nse_data:
            sec_info = nse_data.get("secInfo", {})
            result = {
                "symbol": sym,
                "pe_ratio": _safe(sec_info.get("pe")),
                "forward_pe": None,
                "market_cap": _safe(sec_info.get("marketCap")),
                "dividend_yield": _safe(sec_info.get("dividendYield")),
                "year_change_pct": None,
                "year_high": None,
                "year_low": None,
                "fifty_day_average": None,
                "two_hundred_day_average": None,
                "revenue_growth": None,
                "earnings_growth": None,
                "eps": None,
                "roe": _safe(sec_info.get("roe")),
                "roce": None,
                "debt_to_equity": _safe(sec_info.get("debtToEquity")),
                "free_cashflow": None,
                "profit_margins": None,
                "opm": None,
                "current_ratio": None,
                "interest_coverage": None,
                "held_percent_insiders": None,
                "held_percent_institutions": None,
                "price_to_book": None,
                "pe_growth": None,
                "ev_ebitda": None,
                "promoter_holding": None,
                "promoter_pledging": None,
                "sector": sec_info.get("sector"),
                "industry": sec_info.get("industry"),
            }
            logger.info(f"NSE India fallback fundamentals found for {sym}")

    if result and any(v is not None for v in [result.get("pe_ratio"), result.get("sector"), result.get("market_cap"), result.get("revenue_growth"), result.get("roe")]):
        _fallback_set(cache_key, result)

    if result:
        _cache_set(cache_key, result, ttl=86400)  # fundamentals change slowly, cache 24h
        return result

    stale = _fallback_get(cache_key)
    if stale:
        logger.info(f"Serving fallback fundamentals for {sym} (stale data)")
        stale = dict(stale)
        stale["stale"] = True
        _cache_set(cache_key, stale, ttl=1800)
        return stale

    raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {sym}")


# ── Surprise pre-market baselines (Neon surprise_static_feed) ───────────────
class SurprisePremarketRequest(BaseModel):
    symbols: Optional[list] = None


@app.post("/surprise/premarket")
def surprise_premarket_run(
    body: Optional[SurprisePremarketRequest] = None,
    symbols: Optional[str] = Query(None, description="Comma-separated symbols"),
    background: bool = Query(True, description="Run in background and poll /surprise/premarket/status"),
):
    """
    Pre-compute static baselines for surprise scanner (run ~08:55 IST or manual).
    Default background=true so Render does not hit the 100s gateway timeout.
    Poll GET /surprise/premarket/status for progress %.
    """
    from surprise_premarket import (
        precalculate_surprise_baselines,
        default_universe_from_env,
        get_premarket_progress,
    )
    import threading

    syms: list = []
    if body and body.symbols:
        syms = [str(s).strip() for s in body.symbols if str(s).strip()]
    elif symbols:
        syms = [x.strip() for x in symbols.split(",") if x.strip()]
    if not syms:
        syms = default_universe_from_env()

    prog = get_premarket_progress()
    if prog.get("is_running"):
        return {
            "ok": True,
            "accepted": False,
            "already_running": True,
            "message": "Premarket already running — poll /surprise/premarket/status",
            "progress": prog,
        }

    if background:
        def _job():
            try:
                precalculate_surprise_baselines(syms)
            except Exception as e:
                logger.exception("background premarket: %s", e)

        threading.Thread(target=_job, daemon=True, name="surprise-premarket").start()
        return {
            "ok": True,
            "accepted": True,
            "background": True,
            "symbols": len(syms),
            "message": "Premarket started — poll /surprise/premarket/status",
            "progress": get_premarket_progress(),
        }

    result = precalculate_surprise_baselines(syms)
    return result


@app.get("/surprise/premarket")
def surprise_premarket_get(
    symbols: Optional[str] = Query(None, description="Comma-separated symbols"),
    background: bool = Query(True),
):
    """GET variant for cron curl simplicity."""
    return surprise_premarket_run(body=None, symbols=symbols, background=background)


@app.get("/surprise/premarket/status")
def surprise_premarket_status():
    """Progress for manual UI button (percent, ETA, current symbol)."""
    from surprise_premarket import get_premarket_progress

    return get_premarket_progress()



@app.get("/surprise/static")
def surprise_static_list(limit: int = 50):
    """Peek rows in surprise_static_feed (debug / health)."""
    from surprise_premarket import _db_url

    url = _db_url()
    if not url:
        return {"ok": False, "error": "no_database_url", "rows": []}
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with eng.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT symbol, prev_close, avg_15m_volume, daily_atr, high_52w, "
                        "dist_52w_pct, sector, is_liquid, updated_at "
                        "FROM surprise_static_feed ORDER BY updated_at DESC LIMIT :lim"
                    ),
                    {"lim": max(1, min(limit, 500))},
                )
                .mappings()
                .all()
            )
        eng.dispose()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("updated_at") is not None:
                d["updated_at"] = str(d["updated_at"])
            out.append(d)
        return {"ok": True, "count": len(out), "rows": out}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}



if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)



# ── Official NSE Bhavcopy + quote delivery % (free) ─────────────────────────
@app.get("/delivery/{symbol}")
def get_delivery_pct(symbol: str):
    """
    Accurate delivery % for NSE symbols (free tier).
    Order: Redis cache → NSE quote-equity → official bhavcopy archives → neutral fallback.
    Never fails the request; always returns a structured payload with `source`.
    """
    from bhavcopy import get_delivery

    sym = normalize_symbol(symbol).replace(".NS", "").replace(".BO", "")
    cache_key = f"delivery:{sym}"
    cached = _cache_get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("delivery_pct") is not None:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached

    result = get_delivery(sym)
    result["from_cache"] = False
    # Cache real sources longer outside market hours via existing TTL helper
    ttl = get_cache_ttl()
    if result.get("source") == "fallback_neutral":
        ttl = min(ttl, 900)  # retry sooner when we only had a placeholder
    _cache_set(cache_key, result, ttl=ttl)
    return result


@app.get("/delivery/{symbol}/refresh")
def refresh_delivery_pct(symbol: str):
    """Force-refresh delivery (bypass cache) — useful after market close."""
    from bhavcopy import get_delivery

    sym = normalize_symbol(symbol).replace(".NS", "").replace(".BO", "")
    cache_key = f"delivery:{sym}"
    result = get_delivery(sym)
    result["from_cache"] = False
    _cache_set(cache_key, result, ttl=get_cache_ttl())
    return result

