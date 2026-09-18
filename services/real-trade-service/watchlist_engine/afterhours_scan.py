"""
watchlist_engine/afterhours_scan.py — After-hours news scan (2026-09-17, session57)

Polls Moneycontrol, LiveMint, NDTV Profit, and Business Standard RSS/Atom
feeds (Economic Times removed 2026-09-17, session58 — see the ET / ET-companies
REMOVED comment on _RSS_FEEDS below) once per
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
    NDTVProfit     → +9
    BusinessStandard → +9
    LiveMint       → +8
    (ET/ET-companies removed 2026-09-17, session58 — retired RSS URLs)
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

Feed sources (2026-09-17 fix, session57 — closes a design gap flagged in
session56's own audit): NDTV Profit and Business Standard, both named in
the original design doc, were never actually wired into _RSS_FEEDS —
only Moneycontrol/LiveMint/ET/ET-companies were. Added both. NDTV Profit
serves its feed as Atom (<feed>/<entry>/<published>), not RSS 2.0
(<rss>/<item>/<pubDate>) like the other four — _fetch_rss_items below now
detects and parses either format so this feed (and any future Atom feed)
actually yields items instead of silently parsing to zero via root.iter
("item") finding nothing.

Sentiment veto — cost-context exception (2026-09-17 fix, session57 —
closes the "double-negated headline" gap the session56 docstring flagged
as still open, e.g. "Fall in input costs boosts profit margin"): a plain
membership check for words like "fall"/"decline"/"drop" wrongly vetoed
headlines where the negative word describes a COST going down (unambiguous
good news for margins), not the company's own results going down. Before
vetoing on a negative-outcome keyword, _score_headline now checks whether
that keyword sits immediately next to a cost/expense noun (cost, costs,
expense, expenditure, input, raw material, fuel, price of raw materials,
etc.) within a short word window — if so, that particular match is treated
as a cost-side move, not a results-side one, and does not veto by itself.
This is still keyword/proximity-based, not real NLP or dependency parsing,
so a sufficiently convoluted sentence can still fool it — but it closes
the specific, common pattern named in the prior audit.

Recency filter (2026-09-17 fix, session58 — user request): both RSS
headlines and bulk/block-deal hits are now dropped if they're older than
config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS (env-configurable, clamped to
3–7 days, default 5). RSS items are checked against their own pubDate/
published; bulk-deal hits are checked against the freshest date found in
their nested bulk_deals[].published / insider_transactions[].date. An
item with no parseable date degrades open (kept, not dropped) rather than
being silently discarded — same non-fatal posture as the rest of this
file's best-effort checks.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
    # ET / ET-companies REMOVED (2026-09-17, session58 — live-verified):
    # https://economictimes.indiatimes.com/markets/rss.cms and
    # .../industry/rss.cms both returned HTTP 200 but with
    # content-type text/html and <title>The Economic Times</title> —
    # i.e. ET's normal homepage/section HTML served from Akamai cache
    # (content-msg: DATA_SERVED_FROM_CACHE), not a bot-detection
    # interstitial and not a redirect. These two RSS paths have been
    # retired/repurposed by ET, independent of the User-Agent fix applied
    # earlier this session (which was the correct diagnosis for actual
    # bot-gated feeds, just not what was wrong here). Rather than keep
    # hitting two permanently-dead URLs and logging a warning every
    # AFTERHOURS_SCAN_INTERVAL_SECONDS tick forever, they're removed
    # outright. If ET publishes a working RSS URL again, re-add an entry
    # here with source_bonus 7/6 (unchanged from before) — the rest of
    # the pipeline (symbol extraction, scoring, dedup) needs no changes
    # to pick a re-added ET feed back up.
    {
        # 2026-09-17 fix (session57): named in the original design doc but
        # never actually wired in until now. Served as Atom, not RSS 2.0 —
        # see _fetch_rss_items' format-detection below.
        "source": "NDTVProfit",
        "url": "https://prod-qt-images.s3.amazonaws.com/production/bloombergquint/feed.xml",
        "source_bonus": 9,
    },
    {
        # 2026-09-17 fix (session57): same gap as NDTVProfit above.
        "source": "BusinessStandard",
        "url": "https://www.business-standard.com/rss/markets-106.rss",
        "source_bonus": 9,
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

# 2026-09-17 fix (session57): cost/expense nouns that, when a negative
# keyword sits immediately next to one of these, flip the meaning from
# "the company did badly" to "an input cost went down" — good news, not
# bad. See _score_headline's cost-context check and the module docstring's
# "Sentiment veto — cost-context exception" note for the reasoning and the
# limits of this still-keyword-based approach.
_COST_CONTEXT_WORDS = {
    "cost", "costs", "expense", "expenses", "expenditure", "input",
    "inputs", "raw material", "raw materials", "fuel", "fuel cost",
    "commodity", "commodity prices", "interest cost", "interest costs",
    "borrowing cost", "borrowing costs", "operating cost", "operating costs",
    "material cost", "material costs",
}
# Small set of connector words that, placed between a negative keyword and
# a cost noun (e.g. "fall in input costs"), still count as "next to" for
# the proximity check — kept short and specific rather than a generic
# stopword list, to avoid loosening the check too far.
_COST_CONTEXT_CONNECTORS = {"in", "of", "on", "to"}

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


def _has_uncontextualized_negative(h: str) -> bool:
    """True if `h` (already lowercased) contains a negative-outcome keyword
    that is NOT immediately describing a cost/expense noun going down.

    2026-09-17 fix (session57): a bare `any(k in h for k in
    _NEGATIVE_KEYWORDS)` vetoed headlines like "Fall in input costs boosts
    profit margin" — the negative word ("fall") describes a COST moving
    down, which is good news, not the company's own results moving down.
    This checks a small word-window around each negative-keyword match: if
    a cost/expense noun (optionally through one connector word like "in"/
    "of") appears right next to it, that match is treated as a cost-side
    move and doesn't veto by itself. Still keyword/proximity-based, not
    real parsing — see the module docstring for the acknowledged limits.
    """
    words = re.findall(r"[a-z']+", h)
    for kw in _NEGATIVE_KEYWORDS:
        start = 0
        kw_words = kw.split()
        while True:
            idx = h.find(kw, start)
            if idx == -1:
                break
            start = idx + 1
            # Locate this occurrence in the tokenized word list.
            # Build a rough token index by counting words before idx.
            prefix_word_count = len(re.findall(r"[a-z']+", h[:idx]))
            end_word_idx = prefix_word_count + len(kw_words) - 1
            # Look at up to 3 words after the keyword for a cost noun,
            # allowing one connector word in between.
            window = words[end_word_idx + 1: end_word_idx + 4]
            window_str = " ".join(window)
            is_cost_context = False
            if window and window[0] in _COST_CONTEXT_WORDS:
                is_cost_context = True
            elif (
                len(window) >= 2
                and window[0] in _COST_CONTEXT_CONNECTORS
                and (window[1] in _COST_CONTEXT_WORDS or window_str[len(window[0]) + 1:] in _COST_CONTEXT_WORDS)
            ):
                is_cost_context = True
            if not is_cost_context:
                return True  # a genuine, non-cost-context negative hit
    return False


def _score_headline(headline: str, catalyst_types: list[str], source_bonus: float) -> float:
    """Compute a priority score 0–100 for a headline + its catalyst types."""
    h = headline.lower()
    if _has_uncontextualized_negative(h):
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

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _parse_item_datetime(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a timestamp string into an aware UTC datetime.

    2026-09-17 fix (session58, user request): recency filtering needs a
    single parser that copes with every date shape this file actually
    sees — RSS 2.0's RFC-822 pubDate ("Wed, 16 Sep 2026 21:32:36 +0530"),
    Atom's ISO-8601 published/updated ("2026-01-14T12:23:24.829Z"), and
    the plain "YYYY-MM-DD" dates analysis-intelligence-service's
    bulk_deals/insider_transactions carry. Returns None (not "now") on
    anything unparseable, so callers can tell "confirmed old" apart from
    "unknown" and choose to degrade open on the latter — same non-fatal
    posture as the rest of this file.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    # RFC-822 (RSS pubDate) — e.g. "Wed, 16 Sep 2026 21:32:36 +0530"
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # ISO-8601 (Atom published/updated) — normalize trailing "Z" first,
    # since datetime.fromisoformat() only accepts +00:00-style offsets.
    iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(iso_value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Plain "YYYY-MM-DD" (insider_transactions/bulk_deals date fields)
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_within_max_age(dt: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True if `dt` is within config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS of
    `now` (default: current UTC time). An unparseable/missing `dt` (None)
    is treated as "unknown, not confirmed stale" and passes — same
    degrade-open posture the rest of this file uses for anything it can't
    verify (see e.g. _validate_symbols)."""
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    max_age = timedelta(days=config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS)
    return (now - dt) <= max_age


def _parse_feed_items(root: ET.Element) -> list[dict]:
    """Parse either RSS 2.0 (<rss><channel><item>...) or Atom
    (<feed><entry>...) XML into a common [{title, link, pubDate}] shape.

    2026-09-17 fix (session57): the original version only ever handled RSS
    2.0's <item>/<title>/<link>/<pubDate> shape via root.iter("item"). NDTV
    Profit's actual feed (added this session — see module docstring) is
    Atom: <feed>/<entry>/<title>/<link href="...">/<published>, which has
    no <item> elements at all, so the old code would have silently parsed
    every NDTV Profit fetch to zero items forever without ever raising or
    logging a warning. This checks for RSS <item>s first (unchanged
    behavior for the four existing feeds) and falls back to Atom <entry>s.
    """
    items: list[dict] = []
    rss_items = list(root.iter("item"))
    if rss_items:
        for item in rss_items:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if title:
                items.append({"title": title, "link": link, "pubDate": pub})
        return items

    for entry in root.iter(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        link = ""
        for link_el in entry.iter(f"{_ATOM_NS}link"):
            rel = link_el.get("rel")
            href = link_el.get("href") or ""
            if href and (rel is None or rel == "alternate"):
                link = href
                break
        pub = (entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated") or "").strip()
        if title:
            items.append({"title": title, "link": link, "pubDate": pub})
    return items


# RSS/Atom fetch headers (2026-09-17, session58): every other scraping
# module in this repo (market-data-service/main.py, market-data-service/
# bhavcopy.py, api-gateway/main.py's _NSE_CLIENT_HEADERS, api-gateway/
# ipo_scanner.py, notification-scheduler-service's symbol_master_sync.py,
# ...) sends a browser User-Agent on outbound scrape requests — this module
# was the one exception, so it's added here too for consistency and for any
# feed that DOES bot-gate on a missing UA.
#
# CORRECTION (2026-09-17, later same session — live-verified with curl from
# the deploy VM after this header was already added): the ET/ET-companies
# failure this header was originally written to fix was NOT a bot-detection
# interstitial after all. A direct curl with this exact User-Agent against
# https://economictimes.indiatimes.com/markets/rss.cms returned HTTP 200,
# content-type text/html, <title>The Economic Times</title>, and
# content-msg: DATA_SERVED_FROM_CACHE (genuine Akamai-cached content, not a
# challenge page) — i.e. that RSS URL has simply been retired/repurposed by
# ET to serve their normal homepage HTML, independent of any header sent.
# ET/ET-companies were removed from _RSS_FEEDS below as a result (see the
# comment there). This header is kept regardless: it's still correct
# practice for the remaining feeds and for any bot-gated feed added later,
# just not what was actually wrong with ET.
_RSS_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


async def _fetch_rss_items(feed: dict) -> list[dict]:
    """Fetch one RSS/Atom feed. Returns [] on any error — never crashes the scan."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=_RSS_FETCH_HEADERS) as client:
            resp = await client.get(feed["url"])
            resp.raise_for_status()
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError as pe:
                # 2026-09-17 fix (session58): a 200 response that fails to
                # parse as XML is a bot-detection interstitial, not a
                # transient glitch — log enough of the body to confirm
                # that diagnosis at a glance next time, instead of just the
                # bare ParseError (which by itself gives no way to tell
                # "wrong content" apart from "malformed feed").
                snippet = resp.text[:120].replace("\n", " ")
                logger.warning(
                    "afterhours-scan: %s returned non-XML content (HTTP %d): %s — body starts: %r",
                    feed["source"], resp.status_code, pe, snippet,
                )
                return []
        items = _parse_feed_items(root)
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

    # 2026-09-17 fix (this-session self-review): CircuitBreaker.call()
    # awaits fallback() on both the open-breaker path and the exception
    # path — it must be an async callable. `fallback=lambda: None` is a
    # plain sync lambda; `await fallback()` on it raises TypeError (`await
    # None`), which would have taken down the whole bulk-deal fetch (and,
    # since nothing here catches it, potentially the whole scan tick) the
    # first time api-gateway was slow/down/open-breaker — exactly the
    # condition this fallback exists to handle gracefully.
    async def _fallback():
        return None

    payload = await api_gateway_breaker.call(_call, fallback=_fallback)
    if not payload:
        return {}

    out: dict[str, dict] = {}
    stale_dropped = 0
    for item in (payload.get("bulk_insider_driven") or []):
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        # 2026-09-17 fix (session58, user request): api-gateway's
        # bulk_insider_driven items carry the freshest known dates one
        # level down, in bulk_deals[].published and
        # recent_insider_transactions[].date — the item itself has no
        # top-level timestamp. Take the most recent parseable date across
        # both nested lists and drop the hit if that's older than
        # config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS. If neither list
        # yields a parseable date, degrade open (see _is_within_max_age)
        # rather than discarding a structurally-valid bulk/insider signal
        # just because this pass couldn't date it.
        candidate_dates = [
            _parse_item_datetime(d.get("published"))
            for d in (item.get("bulk_deals") or [])
        ] + [
            _parse_item_datetime(t.get("date"))
            for t in (item.get("insider_transactions") or [])
        ]
        candidate_dates = [d for d in candidate_dates if d is not None]
        most_recent = max(candidate_dates) if candidate_dates else None
        if not _is_within_max_age(most_recent):
            stale_dropped += 1
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
    if stale_dropped:
        logger.debug(
            "afterhours-scan: bulk-deal hits → dropped %d stale (>%dd) hit(s)",
            stale_dropped, config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS,
        )
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
        stale_dropped = 0
        for item in items:
            headline = item["title"]
            # 2026-09-17 fix (session58, user request): drop items whose
            # own pubDate/published is older than
            # config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS — an RSS feed can
            # occasionally re-surface an older story near the top (feed
            # re-publish, CDN cache hiccup) and this file has no other
            # freshness signal once a headline clears the keyword filter.
            # An unparseable/missing date degrades open (see
            # _is_within_max_age) rather than silently dropping items on a
            # feed whose date format this parser doesn't yet recognize.
            item_dt = _parse_item_datetime(item.get("pubDate"))
            if not _is_within_max_age(item_dt):
                stale_dropped += 1
                continue
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
        if stale_dropped:
            logger.debug(
                "afterhours-scan: %s → dropped %d item(s) older than %dd",
                feed["source"], stale_dropped, config.AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS,
            )

    # RSS-derived symbols: if known_symbols (NSE master, 2681 symbols) is
    # available, every symbol in `best` already passed _extract_symbol's
    # membership check — they're confirmed real NSE-EQ tickers.  A secondary
    # live-quote check (_validate_symbols → get_preview_quotes → /quote/…)
    # is WRONG here: NSE is closed during the 15:45–08:45 afterhours window,
    # so yfinance returns null/stale prices for many mid/small-cap symbols
    # (no pre-market NSE data), causing real, valid tickers to be dropped as
    # "unconfirmed" on every overnight scan tick.
    #
    # 2026-09-18 fix (session67): when known_symbols is populated, skip the
    # live-quote validation entirely — symbol-master membership is the correct
    # gate here.  Fall back to the live-quote check only when the master itself
    # is unavailable (known_symbols is empty), which means _extract_symbol
    # already fell back to the old whitelist+heuristic path — in that case the
    # live-quote check is still a useful second filter.
    rss_symbols = list(best.keys())
    if known_symbols:
        # All symbols already confirmed against the real NSE-EQ universe above.
        # No secondary quote-check needed — and it would be wrong afterhours.
        logger.debug(
            "afterhours-scan [%s %s]: %d RSS symbol(s) confirmed via symbol-master (live-quote check skipped — afterhours)",
            mode, market_date, len(rss_symbols),
        )
    else:
        # Fallback path: symbol master was unavailable so _extract_symbol used
        # the old whitelist+heuristic.  Live-quote check is the only secondary
        # filter we have in that degraded state.
        confirmed = await _validate_symbols(rss_symbols)
        dropped = [s for s in rss_symbols if s not in confirmed]
        if dropped:
            logger.info(
                "afterhours-scan [%s %s]: dropped %d unconfirmed symbol(s) "
                "(fallback path — symbol-master was unavailable): %s",
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

    # ── Telegram notification (2026-09-18 fix, session67) ────────────────────
    # Notify on every scan tick (auto or manual) with what was found/updated.
    # Best-effort — a Telegram failure must never fail the scan.
    try:
        from notifier import notify_async
        # Sort by score descending for the notification summary
        sorted_hits = sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True)
        lines = [f"📡 *After-hours scan — {mode}* ({market_date})"]
        if written:
            lines.append(f"{written} row(s) new/updated · {len(best)} total scored this pass\n")
        else:
            lines.append(f"No new rows — {len(best)} symbol(s) already at best score\n")
        for sym, hit in sorted_hits[:8]:  # top 8 in notification
            cat_icon = {"results": "📊", "bulk_block": "🏦", "board": "🗂️", "insider": "👤", "news": "📰"}.get(
                hit["catalyst_type"], "📰"
            )
            lines.append(
                f"{cat_icon} *{sym}* · score {hit['score']:.0f} · {hit['catalyst_type']} · {hit['source']}"
            )
            # Truncate headline to keep message readable
            hl = hit.get("headline", "")
            if hl:
                lines.append(f"   _{hl[:80]}{'…' if len(hl) > 80 else ''}_")
        await notify_async("\n".join(lines))
    except Exception:
        logger.debug("afterhours-scan: Telegram notification failed (non-fatal)", exc_info=True)

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

    # ── Telegram finalize notification (2026-09-18 fix, session67) ───────────
    # Sent once at 08:45 with the final shortlist and full details per symbol.
    # This is the actionable pre-open summary — shows what _prepick will use.
    try:
        from notifier import notify_async
        lines = [
            f"🔔 *After-hours watchlist FINALIZED — {mode}*",
            f"Market date: {market_date}",
            f"Top {len(keep)} candidate(s) for today's open (discarded {len(discard)}):\n",
        ]
        for i, r in enumerate(keep, 1):
            cat_icon = {"results": "📊", "bulk_block": "🏦", "board": "🗂️", "insider": "👤", "news": "📰"}.get(
                r.catalyst_type, "📰"
            )
            lines.append(
                f"{i}. {cat_icon} *{r.symbol}* — score {r.priority_score:.0f} "
                f"[{r.catalyst_type}·{r.catalyst_source}]"
            )
            if r.headline:
                lines.append(f"   _{r.headline[:90]}{'…' if len(r.headline) > 90 else ''}_")
        lines.append("\n_These will be injected as overnight-priority candidates at 09:00 IST._")
        await notify_async("\n".join(lines))
    except Exception:
        logger.debug("afterhours-scan finalize: Telegram notification failed (non-fatal)", exc_info=True)

    return shortlist