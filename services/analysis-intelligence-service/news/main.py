"""
News Intelligence Service - GenAI Enhanced
---------------------------
Uses Hugging Face Inference API for sentiment scoring.
Multiple news sources: Google News, Moneycontrol, Economic Times, Business Standard, NDTV Profit.
v0.5.0 - Multi-source news aggregation.
"""
import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from urllib.parse import quote

import feedparser
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-intelligence-service")

app = FastAPI(title="Stockky News Intelligence Service", version="0.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

HF_API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2"
HF_API_KEY = os.getenv("HF_API_KEY")

# Optional NewsAPI key (free tier)
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

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
    "PWL": "PhysicsWallah",
}


def _company_query(symbol: str) -> str:
    base = symbol.replace(".NS", "").replace(".BO", "").upper()
    return NAME_HINTS.get(base, base)


def _fetch_google_news(symbol: str, max_items: int = 15) -> List[Dict[str, Any]]:
    """Fetch from Google News RSS."""
    query = quote(_company_query(symbol) + " NSE stock")
    feed_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        parsed = feedparser.parse(feed_url)
        items = []
        cutoff = datetime.utcnow() - timedelta(days=7)
        for entry in parsed.entries[:max_items]:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6])
            if published and published < cutoff:
                continue
            items.append({
                "title": entry.title,
                "publisher": getattr(entry.source, "title", "Google News") if hasattr(entry, "source") else "Google News",
                "published": published.isoformat() if published else None,
                "url": entry.link,
            })
        return items
    except Exception as e:
        logger.warning("Google News fetch failed: %s", e)
        return []


def _fetch_moneycontrol(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from Moneycontrol RSS and filter by symbol/keyword."""
    feed_url = "https://www.moneycontrol.com/rss/latestnews.xml"
    try:
        parsed = feedparser.parse(feed_url)
        keyword = _company_query(symbol).lower()
        items = []
        cutoff = datetime.utcnow() - timedelta(days=7)
        for entry in parsed.entries[:30]:
            if keyword in entry.title.lower() or keyword in entry.description.lower():
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
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
        logger.warning("Moneycontrol fetch failed: %s", e)
        return []


def _fetch_economic_times(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from Economic Times RSS (markets section) and filter."""
    feed_url = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
    try:
        parsed = feedparser.parse(feed_url)
        keyword = _company_query(symbol).lower()
        items = []
        cutoff = datetime.utcnow() - timedelta(days=7)
        for entry in parsed.entries[:30]:
            if keyword in entry.title.lower() or keyword in entry.description.lower():
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
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
        logger.warning("Economic Times fetch failed: %s", e)
        return []


def _fetch_business_standard(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from Business Standard RSS (markets) and filter."""
    feed_url = "https://www.business-standard.com/rss/markets-106.rss"
    try:
        parsed = feedparser.parse(feed_url)
        keyword = _company_query(symbol).lower()
        items = []
        cutoff = datetime.utcnow() - timedelta(days=7)
        for entry in parsed.entries[:30]:
            if keyword in entry.title.lower() or keyword in entry.description.lower():
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "Business Standard",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("Business Standard fetch failed: %s", e)
        return []


def _fetch_ndtv_profit(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from NDTV Profit RSS (markets) and filter."""
    feed_url = "https://www.ndtv.com/business/rss"
    try:
        parsed = feedparser.parse(feed_url)
        keyword = _company_query(symbol).lower()
        items = []
        cutoff = datetime.utcnow() - timedelta(days=7)
        for entry in parsed.entries[:30]:
            if keyword in entry.title.lower() or keyword in entry.description.lower():
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "NDTV Profit",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("NDTV Profit fetch failed: %s", e)
        return []


def _fetch_newsapi(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch from NewsAPI (requires NEWSAPI_KEY)."""
    if not NEWSAPI_KEY:
        return []
    query = _company_query(symbol)
    url = f"https://newsapi.org/v2/everything?q={quote(query)}&language=en&sortBy=publishedAt&pageSize={max_items}&apiKey={NEWSAPI_KEY}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                items = []
                cutoff = datetime.utcnow() - timedelta(days=7)
                for article in data.get("articles", []):
                    published = None
                    if article.get("publishedAt"):
                        try:
                            published = datetime.fromisoformat(article["publishedAt"].replace("Z", "+00:00"))
                        except:
                            pass
                    if published and published < cutoff:
                        continue
                    items.append({
                        "title": article.get("title", ""),
                        "publisher": article.get("source", {}).get("name", "NewsAPI"),
                        "published": published.isoformat() if published else None,
                        "url": article.get("url", ""),
                    })
                return items
            else:
                logger.warning("NewsAPI returned status %s", resp.status_code)
                return []
    except Exception as e:
        logger.warning("NewsAPI fetch failed: %s", e)
        return []


def _fetch_headlines(symbol: str, max_items: int = 15) -> List[dict]:
    """Aggregate news from all sources, deduplicate by title, sort by date."""
    all_news = []
    sources = [
        _fetch_google_news,
        _fetch_moneycontrol,
        _fetch_economic_times,
        _fetch_business_standard,
        _fetch_ndtv_profit,
    ]
    if NEWSAPI_KEY:
        sources.append(_fetch_newsapi)

    for source_func in sources:
        try:
            items = source_func(symbol, max_items=10)
            all_news.extend(items)
            logger.info("Fetched %d items from %s", len(items), source_func.__name__)
        except Exception as e:
            logger.warning("Source %s failed: %s", source_func.__name__, e)

    # Deduplicate by title (case-insensitive)
    seen = set()
    unique = []
    for item in all_news:
        title_lower = item["title"].lower()
        if title_lower not in seen:
            seen.add(title_lower)
            unique.append(item)

    # Sort by published date (newest first)
    unique.sort(key=lambda x: x["published"] if x["published"] else "", reverse=True)
    return unique[:max_items]


def _score_headline(title: str) -> float:
    """Call Hugging Face Inference API to get sentiment score."""
    if not HF_API_KEY:
        logger.warning("HF_API_KEY not set; using neutral fallback")
        return 0.0
    try:
        payload = {
            "inputs": f"Classify the sentiment of this stock news headline as positive, negative, or neutral: {title}",
            "parameters": {"max_new_tokens": 10, "temperature": 0.1}
        }
        headers = {
            "Authorization": f"Bearer {HF_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = httpx.post(HF_API_URL, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            result = data[0]['generated_text'].strip().lower()
            if "positive" in result:
                return 0.8
            elif "negative" in result:
                return -0.8
            else:
                return 0.0
        else:
            logger.warning(f"HF API error: {resp.status_code}")
            return 0.0
    except Exception as e:
        logger.warning(f"HF API call failed: {e}")
        return 0.0


@app.get("/")
def root():
    return {
        "service": "Stockky News Intelligence Service",
        "version": "0.5.0",
        "status": "running",
        "model": "Mistral-7B",
        "sources": ["Google News", "Moneycontrol", "Economic Times", "Business Standard", "NDTV Profit", "NewsAPI (optional)"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "news-intelligence-service"}


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    headlines = _fetch_headlines(symbol)

    if not headlines:
        return {
            "symbol": symbol.upper(),
            "news_score": 50,
            "headline_count": 0,
            "reasons": ["No recent news found — treating as neutral, not a signal either way"],
            "headlines": [],
        }

    scored = [(_score_headline(h["title"]), h) for h in headlines]
    avg_sentiment = sum(s for s, _ in scored) / len(scored)

    news_score = round((avg_sentiment + 1) * 50)
    news_score = max(0, min(100, news_score))

    reasons = []
    most_positive = max(scored, key=lambda x: x[0])
    most_negative = min(scored, key=lambda x: x[0])

    if most_negative[0] < -0.3:
        reasons.append(f"Notably negative headline: \"{most_negative[1]['title'][:90]}\"")
    if most_positive[0] > 0.3:
        reasons.append(f"Notably positive headline: \"{most_positive[1]['title'][:90]}\"")
    reasons.append(f"{len(headlines)} recent headlines, average sentiment {'positive' if avg_sentiment > 0.1 else 'negative' if avg_sentiment < -0.1 else 'neutral'}")

    return {
        "symbol": symbol.upper(),
        "news_score": news_score,
        "headline_count": len(headlines),
        "reasons": reasons,
        "headlines": [h for _, h in scored[:6]],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8005))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)