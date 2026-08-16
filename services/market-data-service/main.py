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
    return ((current - previous) / previous) * 100

# Global yfinance concurrency guard (free-tier / Yahoo rate-limit safe).
# Caps concurrent Yahoo calls across quote + history + fundamentals so a
# parallel market scan does not stampede Yahoo and get empty responses.
import threading
_YFINANCE_MAX_CONCURRENT = int(os.getenv("YFINANCE_MAX_CONCURRENT", "6"))
_yf_semaphore = threading.Semaphore(_YFINANCE_MAX_CONCURRENT)
_YF_MIN_INTERVAL = float(os.getenv("YFINANCE_MIN_INTERVAL_SEC", "0.08"))
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

def _with_retry(func, max_retries=2, base_delay=0.5):
    """Retry with exponential backoff – reduced retries for speed."""
    for attempt in range(max_retries):
        try:
            return _yf_rate_limited(func)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = random.uniform(0, base_delay * (2 ** attempt))
            logging.warning(f"Retry {attempt+1}/{max_retries} after {wait:.1f}s: {e}")
            time.sleep(wait)

def is_market_open() -> bool:
    """Return True if current time is within NSE trading hours (Mon-Fri, 09:15-15:30 IST)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

def get_cache_ttl() -> int:
    """Return TTL in seconds: 300 if market open, else 21600 (6 hours)."""
    return 300 if is_market_open() else 21600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-data-service")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
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

# ── Redis cache ────────────────────────────────────────────────────────────────
try:
    if UPSTASH_URL and UPSTASH_TOKEN:
        cache = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
        cache.ping()
        logger.info("Connected to Upstash Redis")
    else:
        raise ValueError("Upstash credentials not set")
except Exception as e:
    logger.warning("Redis unavailable (%s). Running without cache.", e)
    cache = None

def _cache_get(key: str):
    if not cache:
        return None
    val = cache.get(key)
    return json.loads(val) if val else None

def _cache_set(key: str, value: dict, ttl: int = None):
    if not cache:
        return
    if ttl is None:
        ttl = get_cache_ttl()
    cache.setex(key, ttl, json.dumps(value, default=str))

# Fallback cache (30 days)
FALLBACK_TTL_SECONDS = 30 * 24 * 60 * 60

def _fallback_get(key: str):
    if not cache:
        return None
    val = cache.get(f"fallback:{key}")
    return json.loads(val) if val else None

def _fallback_set(key: str, value: dict):
    if not cache:
        return
    cache.setex(f"fallback:{key}", FALLBACK_TTL_SECONDS, json.dumps(value, default=str))

def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
        symbol = f"{symbol}.NS"
    return symbol

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
def health():
    # Lightweight – returns instantly
    return {"status": "ok", "service": "market-data-service", "cache": bool(cache)}

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

@app.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    sym = normalize_symbol(symbol)
    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    # 1. NSE India
    nse_data = _fetch_nse_quote(sym)
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
                "fetched_at": datetime.utcnow().isoformat(),
            }
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

    # 5. Yahoo Raw API
    if price is None:
        price = _fetch_price_from_yahoo_raw(sym)

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
        _cache_set(cache_key, result)
        return result

    # No price – return fallback
    logger.warning(f"Could not fetch price for {sym} from any source. Returning fallback.")
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
    sym = normalize_symbol(symbol)
    cache_key = f"history:{sym}:{period}:{interval}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"
        df = _with_retry(
            lambda: ticker.history(period=period, interval=interval, auto_adjust=True),
            max_retries=2,
            base_delay=0.5,
        )
        if df.empty:
            raise HTTPException(status_code=404, detail=f"No history found for {sym}")

        candles = []
        for idx, row in df.iterrows():
            if _safe(row["Close"]) is None:
                continue
            candles.append({
                "date": idx.strftime("%Y-%m-%d %H:%M"),
                "open": _safe(row["Open"]),
                "high": _safe(row["High"]),
                "low": _safe(row["Low"]),
                "close": _safe(row["Close"]),
                "volume": int(row["Volume"]) if math.isfinite(float(row["Volume"])) else 0,
            })

        if not candles:
            raise HTTPException(status_code=404, detail=f"No valid candles for {sym}")

        result = {"symbol": sym, "period": period, "interval": interval, "candles": candles}
        _cache_set(cache_key, result, ttl=900)  # history can be cached a bit longer (15 min)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch history for %s", sym)
        raise HTTPException(status_code=502, detail=f"Could not fetch history for {sym}: {e}")

@app.get("/fundamentals/{symbol}")
def get_fundamentals_raw(symbol: str):
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

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
            roe = _safe_info("returnOnEquity") * 100 if _safe_info("returnOnEquity") else None
        elif balance_available and "Total Equity Gross Minority Interest" in balance.index:
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if financials_available and "Net Income" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                if equity != 0:
                    roe = (net_income / equity) * 100

        debt_to_equity = None
        if "debtToEquity" in info:
            debt_to_equity = _safe_info("debtToEquity")
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
            profit_margins = _safe_info("profitMargins") * 100
        else:
            if financials_available and "Net Income" in financials.index and "Total Revenue" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                revenue = financials.loc["Total Revenue"].iloc[0]
                if revenue != 0:
                    profit_margins = (net_income / revenue) * 100

        held_percent_institutions = None
        if "heldPercentInstitutions" in info:
            held_percent_institutions = _safe_info("heldPercentInstitutions") * 100
        elif "institutionalPercent" in info:
            held_percent_institutions = _safe_info("institutionalPercent") * 100

        pe_ratio = _safe_info("trailingPE") if "trailingPE" in info else _safe_info("peRatio")
        forward_pe = _safe_info("forwardPE")
        eps = _safe_info("trailingEps") or _safe_info("eps")
        market_cap = _safe_info("marketCap")
        dividend_yield = None
        if "dividendYield" in info:
            dividend_yield = _safe_info("dividendYield") * 100
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
        roce = _safe_info("returnOnCapitalEmployed") * 100 if _safe_info("returnOnCapitalEmployed") else None
        opm = _safe_info("operatingMargins") * 100
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

        promoter_holding = _safe_info("promoterHolding") * 100 if "promoterHolding" in info else None
        promoter_pledging = _safe_info("promoterPledging") * 100 if "promoterPledging" in info else None

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
            "held_percent_insiders": _safe_info("heldPercentInsiders") * 100 if _safe_info("heldPercentInsiders") else None,
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

