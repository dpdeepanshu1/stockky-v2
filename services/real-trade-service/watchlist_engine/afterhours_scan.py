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
  A headline containing any negative-outcome keyword (falls, plunges, probe,
  downgrade, etc.) is vetoed to 0 regardless of positive-keyword matches —
  see _NEGATIVE_KEYWORDS below.

Positive catalyst keywords (headline must contain at least one):
  profit, revenue, growth, results, beat, strong, buy, increase,
  dividend, bonus, buyback, deal, acquisition, fund, raise, order,
  contract, award, approved, launches, expands, q1, q2, q3, q4

Bulk/block-deal candidates (2026-09-17 fix, session56 audit follow-up):
  the original version only polled RSS feeds — bulk/block-deal data was
  promised in the design but never actually wired in. Rather than scrape
  NSE's bulk-deals board directly (api-gateway's _get_bulk_deal_symbols
  already does this and is proven), this pulls the "bulk_insider_driven"
  bucket from api-gateway's existing /stockky-hot endpoint — the same
  endpoint watchlist_engine/sources.py's Tier 1 already calls, through the
  same api_gateway_breaker circuit breaker so a gateway outage degrades to
  "no bulk-deal hits this pass" rather than blocking the RSS side of the
  scan. Bonus: these symbols come pre-resolved by api-gateway, so they
  skip the regex/whitelist symbol-extraction path entirely (see
  _extract_symbol's docstring for why that path is a lower-confidence
  fallback).

Symbol extraction (2026-09-17 fix, session56 audit follow-up — see
symbol_master.py): candidate tokens from RSS headlines are now checked
directly against the real ~2000-symbol NSE-EQ universe (AngelOne's public
scrip master, cached locally) instead of a ~90-ticker hardcoded whitelist
plus a blind length/stoplist heuristic for everything else. This both finds
real short tickers the old 6-char minimum missed (TCS, ITC, SBIN, ONGC...)
and stops non-tickers (generic capitalized English words) from ever being
treated as a symbol in the first place. The old whitelist+heuristic path is
kept only as a degrade-safe fallback for the rare case where the symbol
master itself is completely unavailable (no live fetch, no cached snapshot).

Symbol validation (2026-09-17 fix, session56 audit follow-up):
  RSS-derived candidates only ever exist as a headline string — nothing
  previously confirmed the extracted token was a real, currently-quotable
  NSE equity before it reached NextDayWatchlistEntry (and from there,
  _prepick would happily turn a bogus token into a TradeCandidate). Before
  writing to the DB, every RSS-derived symbol is checked against
  market_feed.feed.get_preview_quotes — a real ticker returns a quote, a
  false-positive extracted word (e.g. "REPORTS", a company's generic
  description word that slipped past the stoplists) does not. Best-effort:
  if market-data-service itself is unavailable, we skip this filter rather
  than block the whole scan (same non-fatal posture as everything else
  here) — bulk-deal-sourced symbols are pre-validated by api-gateway and
  skip this check.
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
import symbol_master
from event_depth_local import classify_text
from resilience.circuit_breaker import api_gateway_breaker

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

# 2026-09-17 fix (session56 audit follow-up): the original scorer only ever
# checked for POSITIVE keywords — "profit falls 20%" and "profit jumps 20%"
# both contain "profit" and scored identically, since nothing checked for a
# negative outcome word. Any of these appearing anywhere in the headline
# vetoes the whole headline to a score of 0, regardless of how many positive
# keywords also matched. This is still a plain keyword check, not real
# sentiment analysis — a genuinely double-negated headline ("fall in costs
# boosts profit") can still slip through — but it closes the large, common
# gap where the base keyword itself (profit/results/strong) appears in both
# good- and bad-news headlines equally.
_NEGATIVE_KEYWORDS = {
    "falls", "fall", "declines", "decline", "drops", "drop", "plunge",
    "plunges", "tumbles", "tumble", "crashes", "crash", "loss", "losses",
    "misses", "miss", "downgrade", "downgraded", "cut", "cuts", "slashed",
    "slash", "probe", "raid", "fraud", "scam", "resigns", "resignation",
    "default", "defaults", "lawsuit", "penalty", "fined", "fine", "ban",
    "banned", "halt", "halted", "suspended", "suspends", "weak", "slump",
    "slumps", "warns", "warning", "layoffs", "layoff", "scandal",
    "negative", "worst", "lowest", "underperform", "downturn", "shrinks",
    "shrink", "widens", "widening", "shutdown", "shuts", "closure",
    "bankruptcy", "insolvency", "default risk", "rating cut", "sell-off",
    "selloff", "crackdown", "scrutiny", "investigation",
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


def _extract_symbol(headline: str, known_symbols: set[str]) -> Optional[str]:
    """Extract a probable NSE symbol from a headline.

    The regex matches ALL-CAPS tokens, so we uppercase the headline first —
    this turns title-case "Reliance" into "RELIANCE" and lets it match
    directly against the real symbol universe. Without this, ~60% of real
    financial headlines (which are title-case, not ALL-CAPS) would yield
    zero tokens.

    When known_symbols (symbol_master.py's real NSE-EQ universe) is
    available, a token is only ever treated as a symbol if it's actually
    in that universe — no length restriction, no guessing. known_symbols
    empty means the master itself is unavailable this pass (no live fetch,
    no cached snapshot) — degrades to the old whitelist+heuristic path
    rather than extracting nothing at all.
    """
    uheadline = headline.upper()
    tokens = _SYMBOL_RE.findall(uheadline)

    if known_symbols:
        for t in tokens:
            if t in known_symbols:
                return t
        return None

    # Degraded fallback — symbol master unavailable this pass.
    for t in tokens:
        if t in _KNOWN_NSE_SYMBOLS:
            return t
    # Min-length 6 (not 3) avoids noisy partial tickers: HDFC (4), BAJAJ (5),
    # SUN (3) etc. that appear in title-case headlines when uppercased.
    for t in tokens:
        if t not in _ALL_STOPS and 6 <= len(t) <= 12:
            return t
    return None


def _score_headline(headline: str, catalyst_types: list[str], source_bonus: float) -> float:
    """Compute a priority score 0–100 for a headline + its catalyst types."""
    h = headline.lower()
    if any(k in h for k in _NEGATIVE_KEYWORDS):
        return 0.0
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


# ── Bulk/block-deal hits via api-gateway's existing /stockky-hot ───────────

async def _fetch_bulk_deal_hits() -> dict[str, dict]:
    """Pull the 'bulk_insider_driven' bucket from api-gateway's /stockky-hot
    (same endpoint + circuit breaker watchlist_engine/sources.py's Tier 1
    already uses). Returns {symbol: {score, headline, catalyst_type, source}}
    — never raises; an open breaker or a bad response just yields {}."""
    async def _call():
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(f"{config.API_GATEWAY_URL}/stockky-hot")
            r.raise_for_status()
            return r.json()

    payload = await api_gateway_breaker.call(_call, fallback=lambda: None)
    if not payload:
        return {}

    out: dict[str, dict] = {}
    for item in (payload.get("bulk_insider_driven") or []):
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        # item["score"] is already a 0-100 conviction score from the same
        # pipeline watchlist_engine/sources.py's Tier 1 takes it from
        # directly (see _normalize_tier1) — no need to re-derive it from
        # keywords the way the RSS path does.
        raw_score = item.get("score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else _CATALYST_BASE_SCORE["bulk_block"]
        score = max(0.0, min(100.0, score))
        if score <= 0:
            continue
        existing = out.get(sym)
        if existing is None or score > existing["score"]:
            out[sym] = {
                "score": score,
                "headline": item.get("summary") or f"{sym}: bulk/block deal + insider activity flagged",
                "catalyst_type": "bulk_block",
                "source": "NSE-bulk-deals",
            }
    logger.debug("afterhours-scan: bulk-deal hits → %d symbol(s)", len(out))
    return out


# ── Symbol validation (RSS-derived symbols only; bulk-deal ones are already
#    resolved server-side by api-gateway) ───────────────────────────────────

async def _validate_symbols(symbols: list[str]) -> set[str]:
    """Best-effort confirmation that each RSS-extracted token is a real,
    currently-quotable NSE equity. Returns the subset that market-data-
    service could produce a price for. On any failure, returns None-like
    behavior handled by the caller (we return the input unchanged so a
    market-data outage doesn't nuke the whole scan pass)."""
    if not symbols:
        return set()
    try:
        from market_feed.feed import get_preview_quotes
        previews = await get_preview_quotes(symbols)
        confirmed = {sym for sym, t in previews.items() if t and t.price and t.price > 0}
        return confirmed
    except Exception:
        logger.exception("afterhours-scan: symbol validation failed (non-fatal — skipping filter this pass)")
        return set(symbols)  # degrade open rather than drop everything


# ── Main scan entry point ────────────────────────────────────────────────────

async def run_afterhours_scan(db, mode: str, market_date: str) -> int:
    """Run one after-hours scan pass for `mode`.

    Fetches all RSS feeds plus api-gateway's bulk/block-deal bucket,
    classifies headlines, scores them, validates RSS-derived symbols against
    a live quote source, and upserts into NextDayWatchlistEntry for
    `market_date` (tomorrow's trading date).

    Returns the total number of new/updated rows written.
    """
    # Load the real NSE-EQ symbol universe once for this pass (in-memory
    # cached inside symbol_master.py, refreshed at most every 24h) — see
    # _extract_symbol's docstring for why this replaced the old whitelist.
    known_symbols = await symbol_master.get_all_symbols(db)
    if not known_symbols:
        logger.warning(
            "afterhours-scan [%s %s]: symbol master unavailable (no live fetch, no "
            "cached snapshot) — falling back to whitelist+heuristic extraction this pass",
            mode, market_date,
        )

    # Collect all scored hits: symbol → best {score, headline, catalyst_type, source}
    best: dict[str, dict] = {}

    for feed in _RSS_FEEDS:
        items = await _fetch_rss_items(feed)
        for item in items:
            headline = item["title"]
            symbol = _extract_symbol(headline, known_symbols)
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

    # RSS-derived symbols are extracted with a regex + small whitelist —
    # confirm each is a real, quotable NSE equity before it can reach
    # NextDayWatchlistEntry (and from there, _prepick → TradeCandidate).
    rss_symbols = list(best.keys())
    confirmed = await _validate_symbols(rss_symbols)
    dropped = [s for s in rss_symbols if s not in confirmed]
    if dropped:
        logger.info(
            "afterhours-scan [%s %s]: dropped %d unconfirmed symbol(s): %s",
            mode, market_date, len(dropped), dropped,
        )
        for s in dropped:
            best.pop(s, None)

    # Bulk/block-deal hits come pre-resolved by api-gateway — merge in,
    # keeping the higher score if a symbol also had an RSS hit.
    bulk_hits = await _fetch_bulk_deal_hits()
    for symbol, hit in bulk_hits.items():
        existing = best.get(symbol)
        if existing is None or hit["score"] > existing["score"]:
            best[symbol] = hit

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
