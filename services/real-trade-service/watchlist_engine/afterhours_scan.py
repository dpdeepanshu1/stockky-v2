"""
watchlist_engine/afterhours_scan.py — After-hours news scan (2026-09-17, session56)

Polls Moneycontrol, LiveMint, and Economic Times RSS feeds once per
AFTERHOURS_SCAN_INTERVAL_SECONDS (default 3600s = hourly) between market
close (~15:45 IST) and pre-market (~08:45 IST next day). Classifies headlines,
scores and deduplicates by symbol, and upserts into NextDayWatchlistEntry so
_prepick can pull them at 09:00 as pre-seeded high-priority candidates.

Design principles:
  - Reuses event_depth_local.classify_text (same keyword matcher Tier 2
    watchlist sources already use) — no new dependencies.
  - One upsert per (mode, symbol, market_date) — keeps the highest
    priority_score across multiple scans so repeated runs only improve the
    ranking, never duplicate rows.
  - RSS feeds are NOT aggressively rate-limited (unlike quote APIs) — hourly
    polling is safe. httpx timeout=10s per feed, failures are logged but
    never crash the scan.
  - A "finalize" pass runs at AFTERHOURS_FINALIZE_TIME_IST (default 08:45)
    to lock the top-N list and log the final shortlist before the market opens.
  - No order placement happens here. Entry at market open (9:15+) is handled
    by _prepick injecting NextDayWatchlistEntry rows as TradeCandidate rows
    (with overnight_priority=True) which then go through the full existing
    pipeline: same risk_engine, same ATR stop/target, same regime gate.

Score formula (0–100):
  base_score by catalyst_type:
    results    → 40   (strong: earnings move stocks hard)
    bulk_block → 35   (institutional signal)
    insider    → 30
    board      → 25
    news       → 15   (generic positive news)
  source_bonus (how trusted the RSS feed is):
    Moneycontrol   → +10
    LiveMint       → +8
    ET             → +7
    ET-companies   → +6
  keyword_bonus: +5 per positive catalyst keyword found in headline (capped +15)
  capped at 100.

Positive catalyst keywords (headline must contain at least one):
  profit, revenue, growth, results, beat, strong, buy, increase,
  dividend, bonus, buyback, deal, acquisition, fund, raise, order,
  contract, award, approved, launches, expands, q1, q2, q3, q4
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
import models
from event_depth_local import classify_text

logger = logging.getLogger("real-trade-afterhours-scan")

_HTTP_TIMEOUT = 10.0

# ── RSS feed definitions ─────────────────────────────────────────────────────
_RSS_FEEDS: list[dict] = [
    {
        "source": "Moneycontrol",
        "url": "https://www.moneycontrol.com/rss/latestnews.xml",
        "source_bonus": 10,
    },
    {
        "source": "LiveMint",
        "url": "https://www.livemint.com/rss/markets",
        "source_bonus": 8,
    },
    {
        "source": "ET",
        "url": "https://economictimes.indiatimes.com/markets/rss.cms",
        "source_bonus": 7,
    },
    {
        "source": "ET-companies",
        "url": "https://economictimes.indiatimes.com/industry/rss.cms",
        "source_bonus": 6,
    },
]

# ── Positive-catalyst keyword filter ────────────────────────────────────────
# A headline must contain at least one of these to be considered for the
# next-day buy list. Keeps the list focused on actionable catalysts.
_POSITIVE_KEYWORDS = {
    "profit", "revenue", "growth", "results", "beat", "strong", "buy",
    "increase", "dividend", "bonus", "buyback", "deal", "acquisition",
    "fund", "raise", "order", "contract", "award", "approved", "launches",
    "expands", "expansion", "record", "highest", "upgrade", "q1", "q2",
    "q3", "q4", "quarterly", "earnings", "raised", "win", "wins", "won",
    "gains", "surge", "jumps", "rallies", "recovery", "turnaround",
}

# Additional keyword-match bonus keywords (each adds +5, capped at +15)
_BONUS_KEYWORDS = {
    "beat": 5, "record": 5, "highest": 5, "upgrade": 5, "acquisition": 5,
    "fund raise": 5, "order win": 5, "strong results": 5, "profit growth": 5,
    "revenue growth": 5, "q4 results": 5, "q3 results": 5, "q2 results": 5,
    "q1 results": 5,
}

# Catalyst-type base scores
_CATALYST_BASE_SCORE: dict[str, float] = {
    "results":    40.0,
    "bulk_block": 35.0,
    "insider":    30.0,
    "board":      25.0,
    "news":       15.0,
}

# ── Symbol extraction ────────────────────────────────────────────────────────
_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,14})\b")
_STOPWORDS = {
    "NSE", "BSE", "IPO", "FII", "DII", "SEBI", "RBI", "MF", "ETF",
    "GDP", "CPI", "PMI", "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ",
    "MOM", "CEO", "CFO", "MD", "NPA", "EMI", "GST", "IT", "US", "UK",
    "EU", "USD", "INR", "SENSEX", "NIFTY", "INDIA", "MARKET", "STOCK",
    "SHARE", "TRADE", "BUY", "SELL", "RISE", "FALL", "OPEN", "CLOSE",
    "HIGH", "LOW", "NEW", "NET", "PAT", "EBIT", "EBITDA", "CAGR",
}
# Common English words >= 6 chars that appear in financial headlines and would
# otherwise pass the length filter as false-positive NSE tickers. Examples:
# "HDFC Bank REPORTS strong quarter" → REPORTS (6 chars, not a ticker).
# This list complements _STOPWORDS (which covers short acronyms) for the
# fallback extraction path.
_ENGLISH_STOPS = {
    "REPORTS", "RESULTS", "PROFIT", "RECORD", "STRONG", "GROWTH", "REVENUE",
    "RAISES", "SECTOR", "STOCKS", "SHARES", "INVEST", "TRADED", "LISTED",
    "PRICES", "RAISED", "SURGES", "TUMBLES", "DECLINES", "ADVANCES",
    "FINANCE", "PHARMA", "ENERGY", "METALS", "BANKING", "CAPITAL",
    "QUARTER", "FISCAL", "ANNUAL", "LOSSES", "INCOME", "MARGIN", "VOLUME",
    "ORDERS", "EXPORT", "IMPORT", "GLOBAL", "LAUNCHES", "EXPANDS",
    "APPROVAL", "APPROVED", "AWARDED", "SIGNED", "CLOSED", "VERSUS",
    "HIGHER", "LOWER", "REPORT", "DIVIDEND", "BUYBACK", "RIGHTS", "BONUS",
    "BLOCK", "LARGE", "SMALL", "MIDCAP", "RALLY", "CRASH", "SURGE",
    "JUMPS", "FALLS", "DROPS", "GAINS",
}
_ALL_STOPS = _STOPWORDS | _ENGLISH_STOPS
_KNOWN_NSE_SYMBOLS = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "KOTAKBANK",
    "SBIN", "AXISBANK", "BAJFINANCE", "BAJAJFINSV", "HCLTECH", "WIPRO",
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "TATAMOTORS", "MARUTI",
    "MM",  # M&M — ampersand not regex-matchable; "MM" won't match in practice but kept as placeholder
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "ONGC", "NTPC",
    "POWERGRID", "COALINDIA", "GRASIM", "ULTRACEMCO", "SHREECEM",
    "TITAN", "NESTLEIND", "BRITANNIA", "DABUR", "HINDUNILVR", "MARICO",
    "GODREJCP", "COLPAL", "EMAMILTD", "ITC", "ASIANPAINT", "BERGER",
    "KANSAINER", "PIDILITIND", "WHIRLPOOL", "VOLTAS", "BLUESTAR",
    "CROMPTON", "HAVELLS", "POLYCAB", "LT", "SIEMENS", "ABB", "BOSCHLTD",
    "CUMMINSIND", "THERMAX", "BHEL", "BEL", "HAL", "NAUKRI", "ZOMATO",
    "PAYTM", "POLICYBZR", "DELHIVERY", "IRCTC", "IRFC", "RVNL",
    "ADANIENT", "ADANIPORTS", "ADANIGREEN", "HINDPETRO", "BPCL", "IOC",
    "GAIL", "PETRONET", "IGL", "MGL", "MPHASIS", "TECHM", "LTIM",
    "LTTS", "PERSISTENT", "COFORGE", "ZYDUSLIFE", "ALKEM", "BIOCON",
    "AUROPHARMA", "NATCOPHARM",
}


def _extract_symbol(headline: str) -> Optional[str]:
    """Extract a probable NSE symbol from a headline.

    The regex matches ALL-CAPS tokens, so we uppercase the headline first —
    this turns title-case "Reliance" into "RELIANCE" and catches it via the
    known-symbol whitelist. Without this, ~60% of real financial headlines
    (which are title-case, not ALL-CAPS) would yield zero tokens.
    """
    uheadline = headline.upper()
    tokens = _SYMBOL_RE.findall(uheadline)
    # Prioritize known NSE symbols (highest confidence)
    for t in tokens:
        if t in _KNOWN_NSE_SYMBOLS:
            return t
    # Fall back to any ALL-CAPS token not in combined stoplist, >= 6 chars.
    # Min-length 6 (not 3) avoids noisy partial tickers: HDFC (4), BAJAJ (5),
    # SUN (3) etc. that appear in title-case headlines when uppercased.
    # Genuine non-whitelisted NSE tickers mentioned in news tend to be 6+ chars
    # (TATASTEEL, PERSISTENT, COALINDIA, AUROPHARMA…).
    for t in tokens:
        if t not in _ALL_STOPS and 6 <= len(t) <= 12:
            return t
    return None


def _score_headline(headline: str, catalyst_types: list[str], source_bonus: float) -> float:
    """Compute a priority score 0–100 for a headline + its catalyst types."""
    h = headline.lower()
    if not any(k in h for k in _POSITIVE_KEYWORDS):
        return 0.0
    base = max(
        (_CATALYST_BASE_SCORE.get(ct, 0.0) for ct in catalyst_types),
        default=_CATALYST_BASE_SCORE["news"],
    )
    kw_bonus = min(15.0, sum(v for kw, v in _BONUS_KEYWORDS.items() if kw in h))
    return min(100.0, base + source_bonus + kw_bonus)


# ── RSS fetch + parse ────────────────────────────────────────────────────────

async def _fetch_rss_items(feed: dict) -> list[dict]:
    """Fetch one RSS feed. Returns [] on any error — never crashes the scan."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(feed["url"])
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if title:
                items.append({"title": title, "link": link, "pubDate": pub})
        logger.debug("afterhours-scan: %s → %d items", feed["source"], len(items))
        return items
    except Exception as e:
        logger.warning("afterhours-scan: RSS fetch failed for %s: %s", feed["source"], e)
        return []


# ── Main scan entry point ────────────────────────────────────────────────────

async def run_afterhours_scan(db, mode: str, market_date: str) -> int:
    """Run one after-hours scan pass for `mode`.

    Fetches all RSS feeds, classifies headlines, scores them, and upserts into
    NextDayWatchlistEntry for `market_date` (tomorrow's trading date).

    Returns the total number of new/updated rows written.
    """
    # Collect all scored hits: symbol → best {score, headline, catalyst_type, source}
    best: dict[str, dict] = {}

    for feed in _RSS_FEEDS:
        items = await _fetch_rss_items(feed)
        for item in items:
            headline = item["title"]
            symbol = _extract_symbol(headline)
            if not symbol:
                continue
            catalyst_types = classify_text(headline) or ["news"]
            score = _score_headline(headline, catalyst_types, feed["source_bonus"])
            if score <= 0:
                continue
            primary_catalyst = catalyst_types[0] if catalyst_types else "news"
            existing = best.get(symbol)
            if existing is None or score > existing["score"]:
                best[symbol] = {
                    "score": score,
                    "headline": headline,
                    "catalyst_type": primary_catalyst,
                    "source": feed["source"],
                }

    if not best:
        logger.info("afterhours-scan [%s %s]: no scored items found this pass", mode, market_date)
        return 0

    now = datetime.now(timezone.utc)
    written = 0

    # 2026-09-17 fix (session56 audit): commit PER SYMBOL instead of once
    # after the whole loop. The previous shape called db.flush() per symbol
    # but db.commit() only once at the end — so if any one symbol's upsert
    # raised (constraint error etc.), the except block's db.rollback() wiped
    # out every other symbol already processed earlier in this same pass,
    # not just the failing one, while the log line still reported the
    # pre-rollback "written" count as if it had succeeded. Committing right
    # after each symbol means a later failure can only roll back its own row.
    for symbol, hit in best.items():
        try:
            existing_row = (
                db.query(models.NextDayWatchlistEntry)
                .filter_by(mode=mode, symbol=symbol, market_date=market_date, consumed=False)
                .first()
            )
            if existing_row is None:
                db.add(models.NextDayWatchlistEntry(
                    mode=mode,
                    symbol=symbol,
                    catalyst_type=hit["catalyst_type"],
                    catalyst_source=hit["source"],
                    headline=hit["headline"],
                    priority_score=hit["score"],
                    market_date=market_date,
                    collected_at=now,
                    consumed=False,
                ))
                db.commit()
                written += 1
            elif hit["score"] > existing_row.priority_score:
                existing_row.priority_score = hit["score"]
                existing_row.catalyst_type = hit["catalyst_type"]
                existing_row.catalyst_source = hit["source"]
                existing_row.headline = hit["headline"]
                existing_row.updated_at = now
                db.commit()
                written += 1
        except Exception as e:
            db.rollback()
            logger.warning(
                "afterhours-scan [%s %s]: failed to upsert %s — skipping: %s",
                mode, market_date, symbol, e,
            )
            continue

    logger.info(
        "afterhours-scan [%s %s]: %d symbol(s) scored → %d rows upserted",
        mode, market_date, len(best), written,
    )
    return written


async def finalize_nextday_watchlist(db, mode: str, market_date: str) -> list[str]:
    """Final-pass at ~08:45 before open: trim to top-N by priority_score,
    mark the rest consumed (discarded), and return the shortlisted symbols."""
    rows = (
        db.query(models.NextDayWatchlistEntry)
        .filter_by(mode=mode, market_date=market_date, consumed=False)
        .all()
    )
    if not rows:
        logger.info("afterhours-scan finalize [%s %s]: nothing to finalize", mode, market_date)
        return []

    rows.sort(key=lambda r: r.priority_score, reverse=True)
    max_picks = config.AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES
    keep = rows[:max_picks]
    discard = rows[max_picks:]

    now = datetime.now(timezone.utc)
    for r in discard:
        r.consumed = True
        r.consumed_at = now
    db.commit()

    shortlist = [r.symbol for r in keep]
    logger.info(
        "afterhours-scan finalize [%s %s]: keeping top %d → %s (discarded %d)",
        mode, market_date, len(keep), shortlist, len(discard),
    )
    return shortlist
