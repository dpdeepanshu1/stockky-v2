"""
News quality upgrade: better aliases (PWL etc.), multi-source aggregation,
relevance filter, and clear summary for frontend.

Import and use from news/main.py:
    from news_quality import expand_keywords, aggregate_and_summarize, build_news_response
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import feedparser
import httpx

logger = logging.getLogger("news_quality")

# Expanded aliases — especially for IPO / edtech / odd ticker names
EXTRA_ALIASES: Dict[str, List[str]] = {
    "PWL": [
        "physics wallah", "physicswallah", "physics-wallah", "pwl",
        "pw limited", "pw edtech", "alakh pandey", "physics wallah limited",
    ],
    "PW": ["physics wallah", "physicswallah", "pwl", "alakh pandey"],
    "PHYSICS WALLAH": ["physics wallah", "pwl", "physicswallah"],
    "MARKSANS": ["marksans pharma", "marksans"],
    "TARSONS": ["tarsons products", "tarsons"],
    "MANORAMA": ["manorama industries", "manorama"],
    "RENAISSANCE": ["renaissance global", "renaissance"],
    "MOTISONS": ["motisons jewellers", "motisons"],
    "LGEINDIA": ["lg electronics india", "lg india", "lg electronics"],
    "LG": ["lg electronics india", "lg india"],
}

# Default free RSS sources (5–10)
def _rss_urls(query: str) -> List[Tuple[str, str]]:
    q = quote(query)
    return [
        ("Google News", f"https://news.google.com/rss/search?q={q}+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
        ("Google News Finance", f"https://news.google.com/rss/search?q={q}+stock+OR+shares+OR+NSE+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
        ("Moneycontrol", f"https://www.moneycontrol.com/rss/latestnews.xml"),  # filtered later
        ("Economic Times", f"https://economictimes.indiatimes.com/markets/stocks/rssreports/2146842.cms"),
        ("LiveMint", f"https://www.livemint.com/rss/markets"),
        ("Business Standard", f"https://www.business-standard.com/rss/markets-106.rss"),
        ("Financial Express", f"https://www.financialexpress.com/market/rss"),
        ("NDTV Profit", f"https://feeds.feedburner.com/ndtvprofit-latest"),
    ]


def expand_keywords(symbol: str, company_name: Optional[str] = None) -> List[str]:
    """Build rich keyword list for relevance matching."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    keys: List[str] = [base.lower()]
    if company_name:
        keys.append(company_name.lower())
        # individual words from name (length >= 4)
        for w in re.split(r"[\s\-,.&]+", company_name):
            if len(w) >= 4:
                keys.append(w.lower())
    for a in EXTRA_ALIASES.get(base, []):
        keys.append(a.lower())
    # also try without Limited / Ltd
    cleaned = []
    for k in keys:
        cleaned.append(k)
        for suffix in (" limited", " ltd", " ltd.", " pvt", " private"):
            if k.endswith(suffix):
                cleaned.append(k[: -len(suffix)].strip())
    # unique preserve order
    seen = set()
    out = []
    for k in cleaned:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _is_relevant(title: str, desc: str, keywords: List[str]) -> bool:
    text = f"{title} {desc}".lower()
    return any(k in text for k in keywords if len(k) >= 2)


def _parse_entries(parsed, publisher: str, keywords: List[str], max_items: int = 8, days: int = 14) -> List[Dict[str, Any]]:
    items = []
    cutoff = datetime.utcnow() - timedelta(days=days)
    for entry in getattr(parsed, "entries", []) or []:
        title = getattr(entry, "title", "") or ""
        desc = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        if not _is_relevant(title, desc, keywords):
            continue
        published = None
        try:
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6])
        except Exception:
            pass
        if published and published < cutoff:
            continue
        items.append({
            "title": title.strip(),
            "description": re.sub(r"<[^>]+>", "", desc)[:400].strip(),
            "url": getattr(entry, "link", "") or "",
            "publisher": publisher,
            "published_at": published.isoformat() if published else None,
        })
        if len(items) >= max_items:
            break
    return items


def fetch_multi_source(
    symbol: str,
    company_name: Optional[str] = None,
    max_per_source: int = 6,
) -> List[Dict[str, Any]]:
    """Fetch from multiple free sources, filter, dedupe."""
    keywords = expand_keywords(symbol, company_name)
    query = company_name or symbol
    all_items: List[Dict[str, Any]] = []

    for publisher, url in _rss_urls(query):
        try:
            # For generic feeds, still filter by keywords
            parsed = feedparser.parse(url)
            items = _parse_entries(parsed, publisher, keywords, max_per_source)
            all_items.extend(items)
        except Exception as e:
            logger.warning("news source %s failed: %s", publisher, e)

    # Dedupe by normalized title
    seen = set()
    unique = []
    for it in all_items:
        key = re.sub(r"\W+", "", (it.get("title") or "").lower())[:80]
        if key and key not in seen:
            seen.add(key)
            unique.append(it)

    # Sort newest first
    def _ts(x):
        try:
            return x.get("published_at") or ""
        except Exception:
            return ""
    unique.sort(key=_ts, reverse=True)
    return unique[:25]


def summarize_headlines(
    items: List[Dict[str, Any]],
    symbol: str,
    max_bullets: int = 5,
    llm_summarizer=None,
) -> str:
    """
    Build a clear short summary.
    llm_summarizer: optional callable(system, user) -> str (Gemini/Groq/HF).
    """
    if not items:
        return f"No recent relevant news found for {symbol}."

    titles = [it["title"] for it in items[:8] if it.get("title")]
    if llm_summarizer:
        try:
            system = (
                "You are a concise equity research assistant for Indian stocks. "
                "Summarize the news in 2–3 short sentences. Focus on business impact, "
                "results, deals, regulation, or growth. No disclaimer."
            )
            user = f"Symbol: {symbol}\nHeadlines:\n- " + "\n- ".join(titles)
            note = llm_summarizer(system, user)
            if note:
                return note.strip()
        except Exception as e:
            logger.warning("LLM news summary failed: %s", e)

    # Fallback extractive
    bullets = titles[:max_bullets]
    return f"Recent news for {symbol}: " + " | ".join(bullets)


def build_news_response(
    symbol: str,
    company_name: Optional[str] = None,
    llm_summarizer=None,
) -> Dict[str, Any]:
    """Full news payload for /analyze or /news endpoints."""
    items = fetch_multi_source(symbol, company_name)
    summary = summarize_headlines(items, symbol, llm_summarizer=llm_summarizer)
    # Simple sentiment proxy from keywords
    pos = sum(1 for it in items if any(w in (it.get("title") or "").lower() for w in (
        "profit", "growth", "surge", "rally", "beat", "win", "deal", "expand", "order"
    )))
    neg = sum(1 for it in items if any(w in (it.get("title") or "").lower() for w in (
        "loss", "fall", "drop", "probe", "fraud", "ban", "delay", "miss", "cut"
    )))
    score = 50.0
    if items:
        score = 50.0 + (pos - neg) * (25.0 / max(len(items), 1))
        score = max(0.0, min(100.0, score))

    return {
        "symbol": symbol.upper(),
        "company_name": company_name,
        "news_score": round(score, 1),
        "summary": summary,
        "headline_count": len(items),
        "headlines": items[:12],
        "sources_checked": 8,
        "keywords_used": expand_keywords(symbol, company_name)[:12],
    }
