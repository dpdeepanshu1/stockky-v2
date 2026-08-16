"""
Event Tracker Service v0.4.4
-----------------------------
Dynamically fetches company name from yfinance for any symbol.
Works for ALL stocks, not just those in NAME_HINTS.

Merged with v0.3.1 features:
- /events/{symbol}/categorized endpoint with upcoming/recent events
- Shared _diff_events logic for /check and /events/{symbol}/categorized
"""
import os
import json
import math
import time
import random
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import quote
from functools import wraps

import yfinance as yf
import feedparser
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("event-tracker-service")

app = FastAPI(title="Stockky Event Tracker Service", version="0.4.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EVENT_CACHE_TTL = 4 * 3600
EMPTY_NEWS_CACHE_TTL = 3600
EVENT_FALLBACK_TTL = 30 * 24 * 3600
STATE_KEY = "stockky:event_state"
EVENT_CACHE_PREFIX = "stockky:event:"
EVENT_FALLBACK_PREFIX = "stockky:event:fallback:"
EVENTS_LIST_CACHE_KEY = "stockky:events_list"
EVENTS_LIST_CACHE_TTL = 3600

# ── In‑memory cache for yfinance calls and company names ──
_yf_cache: Dict[str, Dict[str, Any]] = {}
_company_name_cache: Dict[str, str] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes
COMPANY_NAME_CACHE_TTL = 3600  # 1 hour (company name rarely changes)

def cached_yf(method_name: str):
    """Decorator to cache results of yfinance methods with TTL."""
    def decorator(func):
        @wraps(func)
        def wrapper(symbol: str, *args, **kwargs):
            cache_key = f"{symbol}:{method_name}"
            now = time.time()
            if cache_key in _yf_cache:
                entry = _yf_cache[cache_key]
                if now - entry["timestamp"] < CACHE_TTL_SECONDS:
                    logger.debug(f"Cache hit for {cache_key}")
                    return entry["value"]
                else:
                    del _yf_cache[cache_key]
            logger.debug(f"Cache miss for {cache_key}, calling yfinance")
            result = func(symbol, *args, **kwargs)
            _yf_cache[cache_key] = {"value": result, "timestamp": now}
            return result
        return wrapper
    return decorator


# ── Redis ──────────────────────────────────────────────────────────────────────
_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
    logger.info("Connected to Upstash Redis")
except Exception as e:
    logger.warning("Redis unavailable, caching and persistence disabled: %s", e)


def _redis_get(key: str):
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None


def _redis_set(key: str, value, ttl: int = None):
    if not _redis:
        return
    try:
        data = json.dumps(value, default=str)
        if ttl:
            _redis.setex(key, ttl, data)
        else:
            _redis.set(key, data)
    except Exception as e:
        logger.warning("Redis set failed for %s: %s", key, e)


def _load_state() -> dict:
    return _redis_get(STATE_KEY) or {"subscriptions": [], "last_known": {}}


def _save_state(state: dict):
    _redis_set(STATE_KEY, state)


class SubscribeRequest(BaseModel):
    symbols: List[str]


def _normalize(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"


def _safe_float(val):
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ── Cached yfinance calls ──

_yf_ticker_cache: Dict[str, yf.Ticker] = {}

def _get_ticker(symbol: str) -> yf.Ticker:
    if symbol not in _yf_ticker_cache:
        _yf_ticker_cache[symbol] = yf.Ticker(symbol)
        _yf_ticker_cache[symbol]._tz = "Asia/Kolkata"
    return _yf_ticker_cache[symbol]

def _get_company_name(symbol: str) -> str:
    """Get the long company name from yfinance, with fallback to the symbol."""
    if symbol in _company_name_cache:
        return _company_name_cache[symbol]
    
    ticker = _get_ticker(symbol)
    try:
        info = ticker.info
        name = info.get('longName') or info.get('shortName') or symbol.replace(".NS", "").replace(".BO", "")
        # Cache it
        _company_name_cache[symbol] = name
        logger.info(f"Company name for {symbol}: {name}")
        return name
    except Exception as e:
        logger.warning(f"Could not fetch company name for {symbol}: {e}")
        fallback = symbol.replace(".NS", "").replace(".BO", "")
        _company_name_cache[symbol] = fallback
        return fallback

@cached_yf("get_earnings_dates")
def _get_earnings_dates(symbol: str, limit: int = 1):
    ticker = _get_ticker(symbol)
    try:
        return ticker.get_earnings_dates(limit=limit)
    except Exception as e:
        logger.warning(f"get_earnings_dates failed for {symbol}: {e}")
        return None

@cached_yf("dividends")
def _get_dividends(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.dividends
    except Exception as e:
        logger.warning(f"dividends failed for {symbol}: {e}")
        return None

@cached_yf("splits")
def _get_splits(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.splits
    except Exception as e:
        logger.warning(f"splits failed for {symbol}: {e}")
        return None

@cached_yf("insider_transactions")
def _get_insider_transactions(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.insider_transactions
    except Exception as e:
        logger.warning(f"insider_transactions failed for {symbol}: {e}")
        return None

@cached_yf("upgrades_downgrades")
def _get_upgrades_downgrades(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.upgrades_downgrades
    except Exception as e:
        logger.warning(f"upgrades_downgrades failed for {symbol}: {e}")
        return None

@cached_yf("institutional_holders")
def _get_institutional_holders(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.institutional_holders
    except Exception as e:
        logger.warning(f"institutional_holders failed for {symbol}: {e}")
        return None

@cached_yf("earnings_history")
def _get_earnings_history(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.earnings_history
    except Exception as e:
        logger.warning(f"earnings_history failed for {symbol}: {e}")
        return None

@cached_yf("news")
def _get_news(symbol: str):
    ticker = _get_ticker(symbol)
    try:
        return ticker.news
    except Exception as e:
        logger.warning(f"news failed for {symbol}: {e}")
        return None


# ── Keyword variants (now using company name from yfinance) ──
def _get_keywords(symbol: str) -> List[str]:
    """Return a list of keywords to search in news feeds."""
    company = _get_company_name(symbol)
    base = symbol.replace(".NS", "").replace(".BO", "").upper()
    # Also add the raw symbol as lowercase and uppercase
    return [company, base, base.lower(), company.lower()]


# ── News sources ──

def _fetch_google_news(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from Google News RSS using the company name."""
    company = _get_company_name(symbol)
    query = quote(company)
    feed_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        parsed = feedparser.parse(feed_url)
        logger.info(f"Google News feed entries for {symbol}: {len(parsed.entries)}")
        if getattr(parsed, "bozo", False) and not parsed.entries:
            logger.warning("Google News RSS feed returned empty for %s", symbol)
            return []
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:max_items]:
            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6])
            if published and published < cutoff:
                continue
            items.append({
                "title": entry.title,
                "publisher": getattr(entry.source, "title", None) if hasattr(entry, "source") else "Google News",
                "published": published.isoformat() if published else None,
                "url": entry.link,
            })
        return items
    except Exception as e:
        logger.warning("Failed to fetch Google News for %s: %s", symbol, e)
        return []


def _fetch_moneycontrol_news(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    keywords = _get_keywords(symbol)
    feed_url = "https://www.moneycontrol.com/rss/latestnews.xml"
    try:
        parsed = feedparser.parse(feed_url)
        logger.info(f"Moneycontrol feed entries for {symbol}: {len(parsed.entries)}")
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            text = title + " " + desc
            if any(kw.lower() in text for kw in keywords):
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "Moneycontrol",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("Moneycontrol fetch failed for %s: %s", symbol, e)
        return []


def _fetch_economic_times(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    keywords = _get_keywords(symbol)
    feed_url = "https://economictimes.indiatimes.com/rssfeedstopstories.cms"
    try:
        parsed = feedparser.parse(feed_url)
        logger.info(f"Economic Times feed entries for {symbol}: {len(parsed.entries)}")
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            text = title + " " + desc
            if any(kw.lower() in text for kw in keywords):
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "Economic Times",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("Economic Times fetch failed for %s: %s", symbol, e)
        return []


def _fetch_cnbc_tv18(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    keywords = _get_keywords(symbol)
    feed_url = "https://www.cnbctv18.com/feed/"
    try:
        parsed = feedparser.parse(feed_url)
        logger.info(f"CNBC TV18 feed entries for {symbol}: {len(parsed.entries)}")
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            text = title + " " + desc
            if any(kw.lower() in text for kw in keywords):
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "CNBC TV18",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("CNBC TV18 fetch failed for %s: %s", symbol, e)
        return []


def _fetch_yf_news(symbol: str) -> List[Dict[str, Any]]:
    news_data = _get_news(symbol)
    if not news_data:
        return []
    items = []
    for item in news_data[:5]:
        items.append({
            "title": item.get("content", {}).get("title") or item.get("title", ""),
            "publisher": (item.get("content", {}).get("provider", {}) or {}).get("displayName") or item.get("publisher", ""),
            "published": item.get("content", {}).get("pubDate") or str(item.get("providerPublishTime", "")),
            "url": (item.get("content", {}).get("canonicalUrl", {}) or {}).get("url") or item.get("link", ""),
        })
    return items


def _fetch_news_from_multiple_sources(symbol: str, max_total: int = 15) -> List[Dict[str, Any]]:
    all_news = []

    # 1. Yahoo Finance
    yf_news = _fetch_yf_news(symbol)
    logger.info(f"Yahoo Finance news for {symbol}: {len(yf_news)} items")
    if yf_news:
        all_news.extend(yf_news)

    # 2. Google News
    google_news = _fetch_google_news(symbol, max_items=8)
    logger.info(f"Google News for {symbol}: {len(google_news)} items")
    if google_news:
        all_news.extend(google_news)

    # 3. Moneycontrol
    mc_news = _fetch_moneycontrol_news(symbol, max_items=5)
    logger.info(f"Moneycontrol for {symbol}: {len(mc_news)} items")
    if mc_news:
        all_news.extend(mc_news)

    # 4. Economic Times
    et_news = _fetch_economic_times(symbol, max_items=5)
    logger.info(f"Economic Times for {symbol}: {len(et_news)} items")
    if et_news:
        all_news.extend(et_news)

    # 5. CNBC TV18
    cnbc_news = _fetch_cnbc_tv18(symbol, max_items=5)
    logger.info(f"CNBC TV18 for {symbol}: {len(cnbc_news)} items")
    if cnbc_news:
        all_news.extend(cnbc_news)

    # Deduplicate
    seen = set()
    unique = []
    for item in all_news:
        key = item["title"].strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # Sort by published date (newest first)
    unique.sort(key=lambda x: x.get("published") or "", reverse=True)

    logger.info(f"Total unique news for {symbol} after dedup: {len(unique)}")
    return unique[:max_total]


# ── Core event fetch ──────────────────────────────────────────────────────────
def _fetch_events(symbol: str, force: bool = False) -> dict:
    sym = _normalize(symbol)
    cache_key = f"{EVENT_CACHE_PREFIX}{sym}"

    if not force:
        cached = _redis_get(cache_key)
        if cached and cached.get("recent_news") and len(cached["recent_news"]) > 0:
            logger.info(f"Event cache hit for {sym} with {len(cached['recent_news'])} news")
            return cached
        elif cached:
            logger.info(f"Cache for {sym} has empty news; will fetch fresh")

    logger.info(f"=== Fetching fresh events for {sym} ===")

    # Use cached yfinance calls
    earnings_dates = _get_earnings_dates(sym, limit=1)
    next_earnings = None
    if earnings_dates is not None and not earnings_dates.empty:
        try:
            next_earnings = str(earnings_dates.index[0].date())
        except Exception:
            pass

    divs = _get_dividends(sym)
    last_dividend = None
    if divs is not None and not divs.empty:
        try:
            last_dividend = {
                "date": str(divs.index[-1].date()),
                "amount": _safe_float(divs.iloc[-1]),
            }
        except Exception:
            pass

    splits_data = _get_splits(sym)
    last_split = None
    if splits_data is not None and not splits_data.empty:
        try:
            last_split = {
                "date": str(splits_data.index[-1].date()),
                "ratio": _safe_float(splits_data.iloc[-1]),
            }
        except Exception:
            pass

    ins = _get_insider_transactions(sym)
    recent_insider = []
    if ins is not None and not ins.empty:
        try:
            for _, row in ins.head(3).iterrows():
                recent_insider.append({
                    "date": str(row.get("Start Date", "")) or str(row.name),
                    "insider": str(row.get("Insider", "")),
                    "transaction": str(row.get("Transaction", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row.get("Shares")) else None,
                    "value": _safe_float(row.get("Value")),
                })
        except Exception:
            pass

    ud = _get_upgrades_downgrades(sym)
    recent_analyst = []
    if ud is not None and not ud.empty:
        try:
            ud_sorted = ud.sort_index(ascending=False)
            for _, row in ud_sorted.head(3).iterrows():
                recent_analyst.append({
                    "date": str(row.name.date()) if hasattr(row.name, "date") else str(row.name),
                    "firm": str(row.get("Firm", "")),
                    "to_grade": str(row.get("ToGrade", "")),
                    "from_grade": str(row.get("FromGrade", "")),
                    "action": str(row.get("Action", "")),
                })
        except Exception:
            pass

    ih = _get_institutional_holders(sym)
    institutional_holders = []
    if ih is not None and not ih.empty:
        try:
            for _, row in ih.head(5).iterrows():
                institutional_holders.append({
                    "holder": str(row.get("Holder", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row.get("Shares")) else None,
                    "pct_held": _safe_float(row.get("% Out")),
                })
        except Exception:
            pass

    # ── Multi-source news ──
    recent_news = _fetch_news_from_multiple_sources(sym, max_total=15)

    # Earnings surprise
    earnings_surprise = None
    earnings_history = _get_earnings_history(sym)
    if earnings_history is not None and not earnings_history.empty:
        try:
            latest = earnings_history.iloc[0]
            actual = latest.get("actual")
            estimate = latest.get("estimate")
            if actual is not None and estimate is not None and estimate != 0:
                surprise_pct = ((actual - estimate) / estimate) * 100
                earnings_surprise = {
                    "date": str(latest.name),
                    "actual": _safe_float(actual),
                    "estimate": _safe_float(estimate),
                    "surprise_pct": round(surprise_pct, 2)
                }
        except Exception:
            pass

    bulk_deals = []
    fii_dii_net_flow = None

    result = {
        "symbol": sym,
        "next_earnings_date": next_earnings,
        "last_dividend": last_dividend,
        "last_split": last_split,
        "recent_insider_transactions": recent_insider,
        "recent_analyst_actions": recent_analyst,
        "institutional_holders": institutional_holders,
        "recent_news": recent_news,
        "earnings_surprise": earnings_surprise,
        "bulk_deals": bulk_deals,
        "fii_dii_net_flow": fii_dii_net_flow,
        "checked_at": datetime.utcnow().isoformat(),
        "cached": False,
    }

    fallback_key = f"{EVENT_FALLBACK_PREFIX}{sym}"
    has_real_data = any([
        next_earnings, last_dividend, last_split,
        recent_insider, recent_analyst, institutional_holders, recent_news,
        earnings_surprise, bulk_deals, fii_dii_net_flow,
    ])

    if has_real_data:
        ttl = EVENT_CACHE_TTL if recent_news else EMPTY_NEWS_CACHE_TTL
        if not recent_news:
            logger.info(f"No news for {sym}; caching with short TTL ({ttl}s)")
        _redis_set(cache_key, {**result, "cached": True}, ttl=ttl)
        _redis_set(fallback_key, result, ttl=EVENT_FALLBACK_TTL)
        logger.info(f"Finished fetching events for {sym}: {len(recent_news)} news items")
        return result

    stale = _redis_get(fallback_key)
    if stale:
        logger.info(f"Live fetch for {sym} empty; serving fallback")
        stale = {**stale, "cached": True, "stale": True}
        _redis_set(cache_key, stale, ttl=900)
        return stale

    return result


# ── Shared diff logic (from v0.3.1) ──────────────────────────────────────────
def _diff_events(previous: dict, current: dict) -> list[str]:
    """Compares two event snapshots for one symbol and returns a list of
    human-readable change descriptions. Shared by /check and /events/{symbol}/categorized
    so both surface the same real detected changes.
    """
    diff_reasons = []

    if previous.get("next_earnings_date") != current.get("next_earnings_date"):
        diff_reasons.append(
            f"Earnings date: {previous.get('next_earnings_date')} → {current.get('next_earnings_date')}"
        )

    prev_div = previous.get("last_dividend") or {}
    cur_div = current.get("last_dividend") or {}
    if prev_div.get("date") != cur_div.get("date") and cur_div.get("date"):
        diff_reasons.append(f"New dividend declared: ₹{cur_div.get('amount')} on {cur_div.get('date')}")

    prev_split = previous.get("last_split") or {}
    cur_split = current.get("last_split") or {}
    if prev_split.get("date") != cur_split.get("date") and cur_split.get("date"):
        diff_reasons.append(f"Stock split: {cur_split.get('ratio')}:1 on {cur_split.get('date')}")

    prev_keys = {
        (a.get("date", "") + a.get("firm", ""))
        for a in (previous.get("recent_analyst_actions") or [])
    }
    for action in (current.get("recent_analyst_actions") or []):
        key = action.get("date", "") + action.get("firm", "")
        if key not in prev_keys:
            diff_reasons.append(
                f"Analyst: {action.get('firm')} {action.get('action')} → {action.get('to_grade')}"
            )

    prev_insider_keys = {
        (a.get("date", "") + a.get("insider", ""))
        for a in (previous.get("recent_insider_transactions") or [])
    }
    for txn in (current.get("recent_insider_transactions") or []):
        key = txn.get("date", "") + txn.get("insider", "")
        if key not in prev_insider_keys:
            diff_reasons.append(
                f"Insider {txn.get('transaction')}: {txn.get('insider')} — {txn.get('shares')} shares"
            )

    prev_surprise = previous.get("earnings_surprise") or {}
    cur_surprise = current.get("earnings_surprise") or {}
    if prev_surprise.get("surprise_pct") != cur_surprise.get("surprise_pct"):
        diff_reasons.append(f"Earnings surprise: {cur_surprise.get('surprise_pct')}%")

    prev_bulk = previous.get("bulk_deals") or []
    cur_bulk = current.get("bulk_deals") or []
    if len(cur_bulk) != len(prev_bulk):
        diff_reasons.append("Bulk/Block deal detected")

    # Institutional / mutual fund holding changes
    prev_holders = {h.get("holder"): h for h in (previous.get("institutional_holders") or []) if h.get("holder")}
    cur_holders = {h.get("holder"): h for h in (current.get("institutional_holders") or []) if h.get("holder")}
    for name, cur_h in cur_holders.items():
        prev_h = prev_holders.get(name)
        cur_shares = cur_h.get("shares")
        if prev_h is None and cur_shares:
            diff_reasons.append(f"New institutional holder: {name} — {cur_shares:,} shares")
        elif prev_h is not None and cur_shares and prev_h.get("shares"):
            prev_shares = prev_h.get("shares")
            if cur_shares > prev_shares * 1.05:
                pct_increase = round((cur_shares - prev_shares) / prev_shares * 100, 1)
                diff_reasons.append(
                    f"{name} increased holding by {pct_increase}% "
                    f"({prev_shares:,} → {cur_shares:,} shares)"
                )

    return diff_reasons


# ── Routes ──
@app.get("/")
def root():
    return {
        "service": "Stockky Event Tracker Service",
        "version": "0.4.4",
        "status": "running",
        "endpoints": {
            "/health": "GET",
            "/events/{symbol}": "GET full snapshot",
            "/events/{symbol}/categorized": "GET upcoming/recent events + changes",
            "/events/{symbol}?force=true": "GET bypass cache",
            "/subscribe": "POST",
            "/subscriptions": "GET",
            "/check": "GET",
            "/symbols_with_events": "GET",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "event-tracker-service", "redis": bool(_redis)}


@app.get("/events/{symbol}")
def get_events(symbol: str, force: bool = False):
    return _fetch_events(symbol, force=force)


@app.get("/events/{symbol}/categorized")
def get_events_categorized(symbol: str, force: bool = False):
    """Same underlying data as /events/{symbol}, split into 'upcoming'
    (things that haven't happened yet — next earnings date, if in the
    future) and 'recent' (things that already happened — last dividend,
    last split, recent insider/analyst activity, earnings surprise),
    plus 'recent_changes': real detected changes since the last time
    this symbol was checked, using the same diff logic /check uses.
    """
    symbol = _normalize(symbol)
    current = _fetch_events(symbol, force=force)

    state = _load_state()
    previous = state["last_known"].get(symbol, {})
    recent_changes = _diff_events(previous, current) if previous else []

    upcoming = []
    recent = []

    next_earnings = current.get("next_earnings_date")
    if next_earnings:
        try:
            is_future = datetime.fromisoformat(next_earnings.replace("Z", "")) >= datetime.utcnow()
        except (ValueError, TypeError):
            is_future = True  # unparseable date — don't silently drop it, default to showing it
        (upcoming if is_future else recent).append({
            "type": "earnings_date", "date": next_earnings,
            "description": f"Next earnings: {next_earnings}",
        })

    last_dividend = current.get("last_dividend")
    if last_dividend and last_dividend.get("date"):
        recent.append({
            "type": "dividend", "date": last_dividend.get("date"),
            "description": f"Dividend of ₹{last_dividend.get('amount')} declared",
        })

    last_split = current.get("last_split")
    if last_split and last_split.get("date"):
        recent.append({
            "type": "split", "date": last_split.get("date"),
            "description": f"{last_split.get('ratio')}:1 stock split",
        })

    for action in (current.get("recent_analyst_actions") or []):
        recent.append({
            "type": "analyst", "date": action.get("date"),
            "description": f"{action.get('firm')}: {action.get('action')} → {action.get('to_grade')}",
        })

    for txn in (current.get("recent_insider_transactions") or []):
        recent.append({
            "type": "insider", "date": txn.get("date"),
            "description": f"Insider {txn.get('transaction')}: {txn.get('insider')} — {txn.get('shares')} shares",
        })

    earnings_surprise = current.get("earnings_surprise")
    if earnings_surprise and earnings_surprise.get("date"):
        recent.append({
            "type": "earnings_surprise", "date": earnings_surprise.get("date"),
            "description": f"Earnings surprise: {earnings_surprise.get('surprise_pct')}% vs estimate",
        })

    # Sort each section newest-first where a date is available
    recent.sort(key=lambda x: x.get("date") or "", reverse=True)

    return {
        "symbol": symbol,
        "upcoming": upcoming,
        "recent": recent,
        "recent_changes": recent_changes,
        "institutional_holders": current.get("institutional_holders") or [],
        "checked_at": current.get("checked_at"),
    }


@app.post("/subscribe")
def subscribe(req: SubscribeRequest):
    state = _load_state()
    existing = set(state["subscriptions"])
    for s in req.symbols:
        existing.add(_normalize(s))
    state["subscriptions"] = sorted(existing)
    _save_state(state)
    return {"subscriptions": state["subscriptions"]}


@app.get("/subscriptions")
def list_subscriptions():
    return {"subscriptions": _load_state()["subscriptions"]}


@app.get("/check")
def check_for_changes():
    """Diff each subscribed symbol against last known snapshot.
    Staggered with 1s delay between symbols to avoid Yahoo rate limits.
    Uses the shared _diff_events function.
    """
    state = _load_state()
    changes = []

    for i, symbol in enumerate(state["subscriptions"]):
        if i > 0:
            time.sleep(1)

        current = _fetch_events(symbol)
        previous = state["last_known"].get(symbol, {})
        diff_reasons = _diff_events(previous, current)

        if diff_reasons:
            changes.append({"symbol": symbol, "changes": diff_reasons, "current": current})

        state["last_known"][symbol] = current

    _save_state(state)
    return {
        "checked": len(state["subscriptions"]),
        "changes": changes,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.get("/symbols_with_events")
def symbols_with_events(days_ahead: int = 7):
    """Return a list of subscribed symbols that have an upcoming event
    (earnings, dividend, split) within the next `days_ahead` days.
    The list is cached in Redis for 1 hour.
    """
    cached = _redis_get(EVENTS_LIST_CACHE_KEY)
    if cached and isinstance(cached, list):
        return {"symbols": cached}

    state = _load_state()
    subscriptions = state.get("subscriptions", [])
    if not subscriptions:
        _redis_set(EVENTS_LIST_CACHE_KEY, [], ttl=EVENTS_LIST_CACHE_TTL)
        return {"symbols": []}

    now = datetime.utcnow()
    cutoff = now + timedelta(days=days_ahead)
    result_symbols = []

    for symbol in subscriptions:
        cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
        cached_events = _redis_get(cache_key)
        if not cached_events:
            continue

        next_earnings = cached_events.get("next_earnings_date")
        if next_earnings:
            try:
                dt = datetime.fromisoformat(next_earnings)
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

        last_div = cached_events.get("last_dividend")
        if last_div and last_div.get("date"):
            try:
                dt = datetime.fromisoformat(last_div["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

        last_split = cached_events.get("last_split")
        if last_split and last_split.get("date"):
            try:
                dt = datetime.fromisoformat(last_split["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

    result_symbols = sorted(set(result_symbols))
    _redis_set(EVENTS_LIST_CACHE_KEY, result_symbols, ttl=EVENTS_LIST_CACHE_TTL)
    return {"symbols": result_symbols}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8006))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)