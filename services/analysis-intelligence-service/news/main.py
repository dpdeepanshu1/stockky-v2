"""
News Intelligence Service - GenAI Enhanced
---------------------------
Uses Hugging Face Inference API for sentiment scoring.
Multiple news sources: Google News, Moneycontrol, Economic Times, Business Standard, NDTV Profit.
v0.5.0 - Multi-source news aggregation.
"""
import os
try:
    from news_quality import build_news_response
except Exception:
    build_news_response = None  # type: ignore
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from urllib.parse import quote

import feedparser
import httpx
from fastapi import FastAPI, HTTPException

try:
    # 2026-10-04 (item 9): shared downloader - browser User-Agent, 10 s timeout, status logging, per-URL cache.
    from feed_fetch import fetch_feed
except Exception:  # pragma: no cover - degrade to the old direct feedparser call rather than break start-up
    def fetch_feed(url, source="feed"):  # type: ignore
        return feedparser.parse(url)
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-intelligence-service")


def _utcnow() -> datetime:
    """Naive-UTC 'now' (drop-in for the deprecated datetime.utcnow()).
    Naive on purpose: compared against naive datetimes built from feed timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

app = FastAPI(title="Stockky News Intelligence Service", version="0.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Group 128: Hugging Face retired api-inference.huggingface.co (DNS no longer resolves: boot log showed
# "HF API call failed: [Errno -5] No address associated with hostname" for every headline, so sentiment
# was always the neutral 0.0 fallback). Inference Providers are reached through the OpenAI-compatible router.
# Both are overridable; the model must be one your HF token can use through Inference Providers.
HF_API_URL = (os.getenv("HF_API_URL") or "").strip() or "https://router.huggingface.co/v1/chat/completions"
HF_MODEL = (os.getenv("HF_MODEL") or "").strip() or "mistralai/Mistral-7B-Instruct-v0.2"
# After a network-level failure (DNS/connect/timeout) skip the call for a while instead of retrying per headline.
HF_FAILURE_BACKOFF_SEC = 300.0
_HF_BACKOFF_UNTIL = 0.0
# 2026-10-06 (group 176): a 400/401/403/404/410 from the router is a setup problem (model not served to this
# token, token lacks Inference Providers permission, model retired), not a blip: every headline got the same
# answer and logged "HF API error: 400" again. The call now pauses for HF_ERROR_BACKOFF_SEC (default 1800 s,
# 0 = never pause, blank/invalid = default) and ONE warning carries the first part of HF's own message.
# 429/503 handling is unchanged.
HF_CONFIG_ERROR_STATUSES = (400, 401, 403, 404, 410)


def _hf_error_backoff_sec() -> float:
    raw = (os.getenv("HF_ERROR_BACKOFF_SEC") or "").strip()
    try:
        v = float(raw) if raw else 1800.0
    except ValueError:
        return 1800.0
    return v if v >= 0 else 1800.0
HF_API_KEY = (os.getenv("HF_API_KEY") or "").strip() or None

# Optional NewsAPI key (free tier)
NEWSAPI_KEY = (os.getenv("NEWSAPI_KEY") or "").strip() or None

# ── API-key-in-URL hygiene (session99) ────────────────────────────────────────
# _fetch_newsapi() calls https://newsapi.org/v2/everything?...&apiKey=<key> with
# httpx — the key is part of the URL. httpx logs every request at INFO as
# "HTTP Request: GET <full url> ..." and this service runs
# logging.basicConfig(level=logging.INFO) with nothing muting the "httpx"
# logger, so every news fetch wrote the key to the log. Same class of leak as
# session98's Telegram-bot-token fix (and session99's market-data-service fix);
# same fix shape.
_SECRET_SUBS = (
    (re.compile(r"(?i)([?&]apiKey=)[^&\s'\"]+"), r"\1***"),
)


def _redact_secrets(text) -> str:
    """str(text) with the NewsAPI key above replaced by ``***``. Never raises."""
    try:
        out = str(text)
        for pattern, repl in _SECRET_SUBS:
            out = pattern.sub(repl, out)
        return out
    except Exception:
        return "<text withheld: redaction failed>"


class _SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _redact_secrets(msg)
            if redacted != msg:
                record.msg, record.args = redacted, ()
        except Exception:
            pass   # a logging filter must never break logging
        return True


def _install_httpx_secret_filter() -> None:
    """Idempotent — safe on module reload."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _SecretRedactingFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(_SecretRedactingFilter())


_install_httpx_secret_filter()

# Primary display name + common aliases for keyword matching
NAME_HINTS = {
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "RELIANCE": "Reliance Industries",
    "HCLTECH": "HCL Technologies",
    "COFORGE": "Coforge",
    "ANGELONE": "Angel One",
    "ADANIPOWER": "Adani Power",
    "BEL": "Bharat Electronics",
    "HAL": "Hindustan Aeronautics",
    "TATAMOTORS": "Tata Motors",
    "SBIN": "State Bank of India",
    "PWL": "Physics Wallah",
    "PHYSICS WALLAH": "Physics Wallah",
    "PHYSICSWALLAH": "Physics Wallah",
    "PW": "Physics Wallah",
    "WIPRO": "Wipro",
    "LT": "Larsen & Toubro",
    "ITC": "ITC",
    "BHARTIARTL": "Bharti Airtel",
    "AXISBANK": "Axis Bank",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "BAJFINANCE": "Bajaj Finance",
    "MARUTI": "Maruti Suzuki",
    "TITAN": "Titan",
    "ASIANPAINT": "Asian Paints",
    "NESTLEIND": "Nestle India",
    "ULTRACEMCO": "UltraTech Cement",
    "SUNPHARMA": "Sun Pharma",
    "DRREDDY": "Dr Reddy",
    "CIPLA": "Cipla",
    "POWERGRID": "Power Grid",
    "NTPC": "NTPC",
    "ONGC": "ONGC",
    "COALINDIA": "Coal India",
    "JSWSTEEL": "JSW Steel",
    "TATASTEEL": "Tata Steel",
    "HINDALCO": "Hindalco",
    "ADANIENT": "Adani Enterprises",
    "ADANIPORTS": "Adani Ports",
    "DMART": "Avenue Supermarts",
    "ZOMATO": "Zomato",
    "NYKAA": "Nykaa",
    "PAYTM": "Paytm",
    "POLICYBZR": "Policybazaar",
}

# Extra aliases used only for relevance filtering (not for query)
ALIASES: Dict[str, List[str]] = {
    "PWL": ["physics wallah", "physicswallah", "physics-wallah", "pwl", "pw limited", "pw edtech", "pw skills"],
    "RELIANCE": ["reliance industries", "ril", "reliance"],
    "TCS": ["tata consultancy", "tata consultancy services", "tcs"],
    "INFY": ["infosys", "infy"],
    "HDFCBANK": ["hdfc bank", "hdfcbank"],
    "ICICIBANK": ["icici bank", "icicibank"],
    "SBIN": ["state bank of india", "sbi ", " sbi", "sbin"],
    "ZOMATO": ["zomato", "eternal limited"],
    "DMART": ["dmart", "avenue supermarts"],
    "BHARTIARTL": ["bharti airtel", "airtel"],
    "LT": ["larsen & toubro", "larsen and toubro", "l&t"],
    "LTM": ["l&t finance", "larsen toubro finance"],
    "BAJFINANCE": ["bajaj finance"],
    "BAJAJFINSV": ["bajaj finserv"],
    "ADANIENT": ["adani enterprises"],
    "ADANIPORTS": ["adani ports"],
    "DIXON": ["dixon technologies"],
    "KPITTECH": ["kpit technologies", "kpit"],
    "CUPID": ["cupid limited", "cupid ltd"],
    "YESBANK": ["yes bank"],
    "INDIGO": ["interglobe aviation", "indigo airlines", "goindigo"],
    "IRFC": ["indian railway finance"],
    "HUDCO": ["housing and urban development"],
    "PERSISTENT": ["persistent systems"],
    "COFORGE": ["coforge"],
    "MPHASIS": ["mphasis"],
    "LTTS": ["l&t technology", "ltts"],
    "TECHM": ["tech mahindra"],
    "HCLTECH": ["hcl technologies", "hcl tech"],
    "VEDL": ["vedanta"],
    "TATASTEEL": ["tata steel"],
    "JSWSTEEL": ["jsw steel"],
    "HINDALCO": ["hindalco"],
    "ASIANPAINT": ["asian paints"],
    "TITAN": ["titan company", "titan"],
    "NESTLEIND": ["nestle india", "nestlé"],
    "BRITANNIA": ["britannia"],
    "GODREJCP": ["godrej consumer"],
    "DABUR": ["dabur"],
    "MARICO": ["marico"],
    "PAGEIND": ["page industries"],
    "AUBANK": ["au small finance"],
    "FEDERALBNK": ["federal bank"],
    "BANDHANBNK": ["bandhan bank"],
    "IDFCFIRSTB": ["idfc first bank"],
    "PNB": ["punjab national bank"],
    "CANBK": ["canara bank"],
    "BANKBARODA": ["bank of baroda"],
    "UNIONBANK": ["union bank of india"],
}


def _base_symbol(symbol: str) -> str:
    # Normalise case/whitespace FIRST so a lowercase ".ns" / ".bo" suffix is
    # recognised and stripped like its upper-case form.
    base = symbol.upper().strip()
    for suffix in (".NS", ".BO"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


# 2026-10-04 (startup-log audit, issue 3): only ~45 tickers are in NAME_HINTS, so for every
# other symbol (SHREETNB, DELTAMAGNT, DEVIT, BHARTIHEXA ...) all Google/Reuters queries fell
# back to the bare ticker, which almost never appears in a headline -> "HTTP 200 but 0
# entries" on every source. Resolve the real company name from Yahoo (longName/shortName),
# cache it, and fall back to the ticker only when Yahoo has nothing.
_NAME_CACHE: Dict[str, str] = {}          # base -> resolved name ("" = looked up, nothing found)
_NAME_CACHE_TS: Dict[str, float] = {}     # base -> monotonic time the entry was stored
_NAME_CACHE_LOCK = threading.Lock()
_NAME_HIT_TTL_S = float((os.getenv("NEWS_NAME_CACHE_TTL_S") or "").strip() or "604800")   # names rarely change: 7d
_NAME_MISS_TTL_S = float((os.getenv("NEWS_NAME_MISS_TTL_S") or "").strip() or "3600")   # retry failed lookups hourly
_NAME_LOOKUP_ENABLED = ((os.getenv("NEWS_NAME_LOOKUP") or "").strip() or "1").lower() not in ("0", "false", "no", "off")
_NAME_SUFFIX_RE = re.compile(r"\b(limited|ltd\.?|inc\.?|corporation|corp\.?)\s*$", re.IGNORECASE)


def _clean_company_name(raw: Any, base: str) -> str:
    """Strip a trailing Ltd/Limited so the news query matches how headlines write the name."""
    name = str(raw or "").strip()
    if not name:
        return ""
    name = _NAME_SUFFIX_RE.sub("", name).strip(" .,-")
    # A "name" that is just the ticker adds nothing over the fallback.
    if len(name) < 3 or name.upper().replace(" ", "") == base:
        return ""
    return name


def _lookup_company_name(base: str) -> str:
    """Yahoo longName/shortName for an NSE ticker, cached. Never raises; "" when unknown."""
    if not _NAME_LOOKUP_ENABLED:
        return ""
    now = _mono()
    with _NAME_CACHE_LOCK:
        if base in _NAME_CACHE:
            ttl = _NAME_HIT_TTL_S if _NAME_CACHE[base] else _NAME_MISS_TTL_S
            if now - _NAME_CACHE_TS.get(base, 0.0) < ttl:
                return _NAME_CACHE[base]
    if _yahoo_backoff_active():
        return ""                      # Yahoo is being skipped right now - don't cache a false miss
    name = ""
    try:
        import yfinance as yf
        info = yf.Ticker(f"{base}.NS").info or {}
        name = _clean_company_name(info.get("longName") or info.get("shortName"), base)
    except Exception as e:
        logger.debug("company name lookup %s: %s", base, e)
    with _NAME_CACHE_LOCK:
        _NAME_CACHE[base] = name
        _NAME_CACHE_TS[base] = _mono()
    return name


def _company_query(symbol: str) -> str:
    base = _base_symbol(symbol)
    hint = NAME_HINTS.get(base)
    if hint:
        return hint
    return _lookup_company_name(base) or base


def _match_keywords(symbol: str) -> List[str]:
    """All lowercased keywords that indicate relevance for this symbol.

    Short tokens (<=2 chars) are excluded to avoid false positives
    (e.g. "pw" matching every headline). Prefer multi-word aliases for
    names like Physics Wallah / PWL.
    """
    base = _base_symbol(symbol)
    keys = set()
    if len(base) >= 3:
        keys.add(base.lower())
    name = _company_query(symbol)
    keys.add(name.lower())
    # Split multi-word names — keep parts with length > 2 only
    for part in name.lower().replace("&", " ").replace("-", " ").split():
        if len(part) > 2:
            keys.add(part)
    for a in ALIASES.get(base, []):
        a_l = a.lower().strip()
        if len(a_l) >= 3:
            keys.add(a_l)
    # Compact form without spaces
    compact = name.lower().replace(" ", "").replace("&", "")
    if len(compact) >= 3:
        keys.add(compact)
    return list(keys)


def _is_relevant(title: str, description: str, keywords: List[str]) -> bool:
    """Relevance filter: prefer multi-word / longer keywords; avoid tiny token noise."""
    import re
    text = f"{title or ''} {description or ''}".lower()
    if not text.strip():
        return False
    # Prefer any multi-word alias or keyword length >= 5 as strong match
    strong = [k for k in keywords if len(k) >= 5 or " " in k]
    weak = [k for k in keywords if 3 <= len(k) < 5 and " " not in k]
    if any(k in text for k in strong):
        return True
    # Weak (short tickers): require word-boundary match
    for k in weak:
        if re.search(rf"\b{re.escape(k)}\b", text):
            return True
    return False


def _parse_feed_items(parsed, publisher: str, keywords: List[str], max_items: int, days: int = 10) -> List[Dict[str, Any]]:
    items = []
    cutoff = _utcnow() - timedelta(days=days)
    for entry in getattr(parsed, "entries", [])[:50]:
        title = getattr(entry, "title", "") or ""
        desc = getattr(entry, "description", "") or getattr(entry, "summary", "") or ""
        if not _is_relevant(title, desc, keywords):
            continue
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6])
            except Exception:
                pass
        if published and published < cutoff:
            continue
        items.append({
            "title": title,
            "publisher": publisher,
            "published": published.isoformat() if published else None,
            "url": getattr(entry, "link", "") or "",
            "snippet": (desc[:220] + "…") if len(desc) > 220 else desc,
        })
        if len(items) >= max_items:
            break
    return items



# Yahoo news backoff (group108, log-audit item 9). From the VM Yahoo news often returns nothing for every
# symbol, and each symbol still paid a yfinance call (and its network wait) before the other sources ran.
# After YAHOO_NEWS_BACKOFF_AFTER results in a row that are empty or raised, Yahoo is skipped for
# YAHOO_NEWS_BACKOFF_SECONDS; the first non-empty result resets the count. Only Yahoo is skipped: every
# other headline source still runs, so what comes back is unchanged except for the wasted Yahoo attempts.
# Set YAHOO_NEWS_BACKOFF_AFTER=0 to turn the backoff off. State is per process.
_YAHOO_BACKOFF_AFTER = int(((os.getenv("YAHOO_NEWS_BACKOFF_AFTER") or "").strip() or "10"))
_YAHOO_BACKOFF_SECONDS = float(((os.getenv("YAHOO_NEWS_BACKOFF_SECONDS") or "").strip() or "600"))
_yahoo_lock = threading.Lock()
_yahoo_state: Dict[str, Any] = {"misses": 0, "skip_until": 0.0}
_mono = time.monotonic


def _yahoo_backoff_active() -> bool:
    if _YAHOO_BACKOFF_AFTER <= 0:
        return False
    with _yahoo_lock:
        return _mono() < _yahoo_state["skip_until"]


def _yahoo_record(got_items: bool) -> None:
    """Count one Yahoo attempt; open the backoff window after enough consecutive misses."""
    if _YAHOO_BACKOFF_AFTER <= 0:
        return
    with _yahoo_lock:
        if got_items:
            _yahoo_state["misses"] = 0
            return
        _yahoo_state["misses"] += 1
        if _yahoo_state["misses"] >= _YAHOO_BACKOFF_AFTER:
            _yahoo_state["misses"] = 0
            _yahoo_state["skip_until"] = _mono() + max(0.0, _YAHOO_BACKOFF_SECONDS)
            logger.warning(
                "Yahoo news: %d empty/failed lookups in a row - skipping Yahoo for %.0fs (other sources unaffected)",
                _YAHOO_BACKOFF_AFTER, _YAHOO_BACKOFF_SECONDS,
            )


def _fetch_yahoo_news(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """LEVEL 1: Yahoo Finance news (free, often rate-limited → fall through)."""
    if _yahoo_backoff_active():
        return []
    base = _base_symbol(symbol)
    items: List[Dict[str, Any]] = []
    try:
        import yfinance as yf
        tkr = yf.Ticker(f"{base}.NS")
        raw = getattr(tkr, "news", None) or []
        for n in raw[:max_items]:
            if not isinstance(n, dict):
                continue
            # yfinance news shapes vary (content / title)
            content = n.get("content") if isinstance(n.get("content"), dict) else {}
            title = n.get("title") or content.get("title") or ""
            link = (
                n.get("link")
                or (content.get("clickThroughUrl") or {}).get("url")
                or (content.get("canonicalUrl") or {}).get("url")
                or ""
            )
            if not title:
                continue
            items.append({
                "title": title,
                "link": link,
                "publisher": n.get("publisher") or content.get("provider", {}).get("displayName") or "Yahoo",
                "published": str(n.get("providerPublishTime") or content.get("pubDate") or ""),
                "source": "yahoo",
            })
    except Exception as e:
        logger.debug("yahoo news %s: %s", base, e)
    _yahoo_record(bool(items))
    return items


def _fetch_google_news(symbol: str, max_items: int = 15) -> List[Dict[str, Any]]:
    """Fetch from Google News RSS with company name + aliases."""
    name = _company_query(symbol)
    base = _base_symbol(symbol)
    queries = [
        f'"{name}" NSE OR BSE stock OR shares',
        f"{base} NSE stock",
    ]
    # Extra query for known aliases (e.g. Physics Wallah / PWL)
    for a in ALIASES.get(base, [])[:2]:
        queries.append(f'"{a}" stock OR IPO OR results')
    keywords = _match_keywords(symbol)
    all_items = []
    for q in queries[:3]:
        feed_url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            parsed = fetch_feed(feed_url, "news")
            all_items.extend(_parse_feed_items(parsed, "Google News", keywords, max_items, days=14))
        except Exception as e:
            logger.warning("Google News fetch failed for %s: %s", q, e)
    return all_items[:max_items]


def _fetch_moneycontrol(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://www.moneycontrol.com/rss/latestnews.xml"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "Moneycontrol", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("Moneycontrol fetch failed: %s", e)
        return []


def _fetch_economic_times(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "Economic Times", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("Economic Times fetch failed: %s", e)
        return []


def _fetch_business_standard(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://www.business-standard.com/rss/markets-106.rss"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "Business Standard", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("Business Standard fetch failed: %s", e)
        return []


def _fetch_ndtv_profit(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://prod-qt-images.s3.amazonaws.com/production/bloombergquint/feed.xml"  # 2026-10-04: www.ndtv.com/business/rss is not a live feed; this Atom feed is the one afterhours_scan verified live
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "NDTV Profit", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("NDTV Profit fetch failed: %s", e)
        return []


def _fetch_livemint(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://www.livemint.com/rss/markets"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "LiveMint", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("LiveMint fetch failed: %s", e)
        return []


def _fetch_financial_express(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    feed_url = "https://www.financialexpress.com/market/feed/"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "Financial Express", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("Financial Express fetch failed: %s", e)
        return []


def _fetch_reuters_india(symbol: str, max_items: int = 8) -> List[Dict[str, Any]]:
    """Google News restricted to Reuters for India stocks."""
    name = _company_query(symbol)
    q = quote(f"{name} site:reuters.com")
    feed_url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        parsed = fetch_feed(feed_url, "news")
        return _parse_feed_items(parsed, "Reuters", _match_keywords(symbol), max_items)
    except Exception as e:
        logger.warning("Reuters proxy fetch failed: %s", e)
        return []


def _fetch_newsapi(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    if not NEWSAPI_KEY:
        return []
    query = _company_query(symbol)
    url = (
        f"https://newsapi.org/v2/everything?q={quote(query)}"
        f"&language=en&sortBy=publishedAt&pageSize={max_items}&apiKey={NEWSAPI_KEY}"
    )
    keywords = _match_keywords(symbol)
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                logger.warning("NewsAPI returned status %s", resp.status_code)
                if resp.status_code in (429, 403, 503):
                    try:
                        from rate_limit_report import record_rate_limit_hit
                        record_rate_limit_hit(
                            provider="analysis",
                            status=resp.status_code,
                            path="news/newsapi",
                        )
                    except Exception:
                        pass
                return []
            data = resp.json()
            items = []
            cutoff = _utcnow() - timedelta(days=10)
            for article in data.get("articles", []):
                title = article.get("title") or ""
                desc = article.get("description") or ""
                if not _is_relevant(title, desc, keywords):
                    continue
                published = None
                if article.get("publishedAt"):
                    try:
                        published = datetime.fromisoformat(article["publishedAt"].replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        pass
                if published and published < cutoff:
                    continue
                items.append({
                    "title": title,
                    "publisher": (article.get("source") or {}).get("name", "NewsAPI"),
                    "published": published.isoformat() if published else None,
                    "url": article.get("url", ""),
                    "snippet": (desc[:220] + "…") if len(desc) > 220 else desc,
                })
            return items
    except Exception as e:
        logger.warning("NewsAPI fetch failed: %s", _redact_secrets(e))
        return []


def _fetch_headlines(symbol: str, max_items: int = 15) -> List[dict]:
    """Aggregate from 5–10 free sources, filter by relevance, dedupe, sort."""
    all_news = []
    sources = [
        _fetch_yahoo_news,   # LEVEL 1
        _fetch_google_news,  # LEVEL 2 waterfall
        # _fetch_moneycontrol,        # 2026-10-04: HTTP 403 from the VM (bot-gated) - re-add if unblocked
        _fetch_economic_times,
        _fetch_business_standard,
        _fetch_ndtv_profit,
        _fetch_livemint,
        # _fetch_financial_express,   # 2026-10-04: HTTP 403 from the VM (bot-gated) - re-add if unblocked
        _fetch_reuters_india,
    ]
    if NEWSAPI_KEY:
        sources.append(_fetch_newsapi)

    # Group 129: this used to log one INFO line per source (7+ lines per symbol, hundreds per
    # stockky-hot pass). One summary line per symbol now; the per-source line is DEBUG.
    per_source = []
    for source_func in sources:
        short = source_func.__name__.replace("_fetch_", "")
        try:
            items = source_func(symbol, max_items=10)
            all_news.extend(items)
            per_source.append(f"{short}={len(items)}")
            logger.debug("Fetched %d items from %s", len(items), source_func.__name__)
        except Exception as e:
            per_source.append(f"{short}=failed")
            logger.warning("Source %s failed: %s", source_func.__name__, e)
    logger.info("news sources for %s: %s (total %d)", symbol, " ".join(per_source), len(all_news))

    seen = set()
    unique = []
    for item in all_news:
        title_lower = (item.get("title") or "").lower().strip()
        if not title_lower or title_lower in seen:
            continue
        seen.add(title_lower)
        unique.append(item)

    unique.sort(key=lambda x: x.get("published") or "", reverse=True)
    return unique[:max_items]


def _summarize_headlines(headlines: List[dict], symbol: str) -> str:
    """Produce a short, clear summary of important news for the frontend."""
    if not headlines:
        return f"No recent relevant news found for {_company_query(symbol)}."
    name = _company_query(symbol)
    top = headlines[:4]
    # Theme tags from titles
    themes = []
    blob = " ".join((h.get("title") or "").lower() for h in top)
    for label, kws in (
        ("Results/earnings", ["result", "earnings", "profit", "revenue", "q1", "q2", "q3", "q4"]),
        ("Deal/order", ["order", "deal", "contract", "wins", "bagged"]),
        ("Management/stake", ["promoter", "stake", "buyback", "insider", "bulk", "block"]),
        ("Guidance/outlook", ["guidance", "outlook", "raises", "cuts", "forecast"]),
    ):
        if any(k in blob for k in kws):
            themes.append(label)
    theme_line = ("Themes: " + ", ".join(themes) + ". ") if themes else ""
    bullets = []
    for h in top:
        pub = h.get("publisher") or "Source"
        title = (h.get("title") or "").strip()
        if title:
            if len(title) > 110:
                title = title[:107] + "…"
            bullets.append(f"• [{pub}] {title}")
    joined = "\n".join(bullets)
    return f"{name}: {theme_line}{len(headlines)} relevant item(s).\n{joined}"


def _score_headline(title: str) -> float:
    """Call Hugging Face Inference Providers (router, chat-completions) to get a sentiment score."""
    global _HF_BACKOFF_UNTIL
    if not HF_API_KEY:
        logger.warning("HF_API_KEY not set; using neutral fallback")
        return 0.0
    if time.monotonic() < _HF_BACKOFF_UNTIL:
        return 0.0
    try:
        payload = {
            "model": HF_MODEL,
            "messages": [{
                "role": "user",
                "content": (
                    "Classify the sentiment of this stock news headline as positive, negative, "
                    f"or neutral. Answer with one word: {title}"
                ),
            }],
            "max_tokens": 10,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {HF_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = httpx.post(HF_API_URL, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            result = str(data["choices"][0]["message"]["content"]).strip().lower()
            if "positive" in result:
                return 0.8
            elif "negative" in result:
                return -0.8
            else:
                return 0.0
        else:
            if resp.status_code in HF_CONFIG_ERROR_STATUSES and _hf_error_backoff_sec() > 0:
                _pause = _hf_error_backoff_sec()
                _HF_BACKOFF_UNTIL = time.monotonic() + _pause
                try:
                    _detail = " ".join(str(getattr(resp, "text", "") or "").split())[:200]
                except Exception:  # noqa: BLE001
                    _detail = ""
                logger.warning(
                    "HF API error: %s%s - check HF_MODEL (%s) is served to your token through Inference "
                    "Providers and the token may call them; skipping Hugging Face sentiment for %ds",
                    resp.status_code, f" ({_detail})" if _detail else "", HF_MODEL, int(_pause),
                )
            else:
                logger.warning(f"HF API error: {resp.status_code}")
            if resp.status_code in (429, 503):
                try:
                    from rate_limit_report import record_rate_limit_hit
                    record_rate_limit_hit(
                        provider="analysis",
                        status=resp.status_code,
                        path="news/huggingface",
                    )
                except Exception:
                    pass
            return 0.0
    except Exception as e:
        if isinstance(e, httpx.TransportError):
            _HF_BACKOFF_UNTIL = time.monotonic() + HF_FAILURE_BACKOFF_SEC
            logger.warning(
                f"HF API call failed: {e} - skipping Hugging Face sentiment for {int(HF_FAILURE_BACKOFF_SEC)}s"
            )
        else:
            logger.warning(f"HF API call failed: {e}")
        try:
            from rate_limit_report import report_if_rate_limited
            report_if_rate_limited(e, provider="analysis", path="news/huggingface")
        except Exception:
            pass
        return 0.0


@app.get("/")
def root():
    return {
        "service": "Stockky News Intelligence Service",
        "version": "0.6.0",
        "status": "running",
        "model": "Mistral-7B + keyword relevance",
        "sources": [
            "Google News", "Moneycontrol", "Economic Times", "Business Standard",
            "NDTV Profit", "LiveMint", "Financial Express", "Reuters (via Google News)",
            "NewsAPI (optional)",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "news-intelligence-service"}


@app.get("/analyze/{symbol}")
def analyze(symbol: str, company_name: str | None = None, force: bool = False):
    # INTEGRATION: news_quality multi-source + better summary (fallback to legacy below)
    # 2026-10-04 (item 9): without a company name the quality path only matched the bare ticker ("tcs"), so a
    # headline saying "Tata Consultancy Services ..." was never found. Use the known display name when we have one.
    company_name = company_name or NAME_HINTS.get(_base_symbol(symbol)) or _lookup_company_name(_base_symbol(symbol)) or None
    if build_news_response is not None:
        try:
            payload = build_news_response(symbol, company_name=company_name, llm_summarizer=None)
            if isinstance(payload, dict) and (payload.get("headline_count") or payload.get("headlines")):
                return payload
        except Exception as e:
            logger.warning("news_quality path failed, using legacy: %s", e)

    headlines = _fetch_headlines(symbol, max_items=15)
    summary = _summarize_headlines(headlines, symbol)

    if not headlines:
        return {
            "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
            "news_score": 50,
            "headline_count": 0,
            "reasons": ["No recent relevant news found — treating as neutral"],
            "summary": summary,
            "headlines": [],
            "data_quality": {
                "level": "none",
                "sources_used": [],
                "hf_sentiment": bool(HF_API_KEY),
                "note": "No headline matched from any source - score is a neutral placeholder, not a reading",
            },
        }

    scored = [(_score_headline(h["title"]), h) for h in headlines]
    avg_sentiment = sum(s for s, _ in scored) / len(scored)

    news_score = round((avg_sentiment + 1) * 50)
    news_score = max(0, min(100, news_score))

    reasons = []
    most_positive = max(scored, key=lambda x: x[0])
    most_negative = min(scored, key=lambda x: x[0])

    if most_negative[0] < -0.3:
        reasons.append(f"Notably negative: \"{most_negative[1]['title'][:90]}\"")
    if most_positive[0] > 0.3:
        reasons.append(f"Notably positive: \"{most_positive[1]['title'][:90]}\"")
    tone = "positive" if avg_sentiment > 0.1 else "negative" if avg_sentiment < -0.1 else "neutral"
    reasons.append(f"{len(headlines)} relevant headlines, average sentiment {tone}")

    return {
        "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
        "news_score": news_score,
        "headline_count": len(headlines),
        "data_quality": {
            "level": (
                "high" if len(headlines) >= 4
                else "medium" if len(headlines) >= 2
                else "low" if len(headlines) >= 1
                else "none"
            ),
            "sources_used": list({(h.get("publisher") or "unknown") for h in headlines}),
            "hf_sentiment": bool(HF_API_KEY),
            "note": (
                "Multiple corroborating headlines"
                if len(headlines) >= 4
                else "Limited free-source coverage — treat score as soft"
                if len(headlines) < 2
                else "Adequate free-source coverage"
            ),
        },
        "reasons": reasons,
        "summary": summary,
        "headlines": [h for _, h in scored[:8]],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(((os.getenv("PORT") or "").strip() or 8005))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)