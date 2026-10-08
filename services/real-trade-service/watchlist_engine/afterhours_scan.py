"""
watchlist_engine/afterhours_scan.py — After-hours news scan (2026-09-17, session57)

Polls Moneycontrol, LiveMint, NDTV Profit, and Business Standard RSS/Atom
feeds (Economic Times removed 2026-09-17, session58 — see the ET / ET-companies
REMOVED comment on _RSS_FEEDS below) on a time-of-day cadence
(every 6 h off-market, every 30 min 08:00-09:00 IST; see auto_pilot._afterhours_next_sleep_seconds) between market
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

import html as _html
import logging
import re
import time
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

# Telegram alert size bounds (2026-10-04). Long messages are split into parts by
# notification-scheduler-service, so these are only sanity caps.
_NOTIFY_MAX_SYMBOLS = 30
_NOTIFY_MAX_HEADLINE = 400

_HTTP_TIMEOUT = 10.0

# ── RSS feed definitions ─────────────────────────────────────────────────────
_RSS_FEEDS: list[dict] = [
    {
        "source": "Moneycontrol",
        "url": "https://www.moneycontrol.com/rss/latestnews.xml",
        "source_bonus": 10,
        # 2026-10-08 (group 238): moneycontrol.com answers this VM's IP with HTTP 403
        # (Akamai bot gate), so the scan got 0 items from the highest-weighted feed.
        # Tried in order ONLY when the primary URL fails: another Moneycontrol feed path,
        # then a Google News RSS search restricted to moneycontrol.com (Google does not
        # bot-gate RSS; its titles end with " - Moneycontrol", stripped on read).
        # NOT live-verified from this sandbox (no network) - check the next scan log line
        # "Moneycontrol primary feed unavailable ... served by fallback".
        "fallback_urls": [
            "https://www.moneycontrol.com/rss/business.xml",
            "https://news.google.com/rss/search?q=site:moneycontrol.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
        ],
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


# 2026-09-24 fix (user report: real movers with genuine news — Man
# Industries, Everest Kanto Cylinder, OLA Electric, Raymond Realty,
# Fujiyama Power Systems — never appearing in the after-hours scan even
# on days with obvious coverage). Root cause: _extract_symbol only ever
# matched a headline's ALL-CAPS *tokens* against the ticker string itself
# ("RELIANCE" in "Reliance surges" works because the company's common
# name and its ticker are the same word). That silently fails for any
# stock whose ticker is an abbreviation/rename that never appears as a
# literal word in ordinary prose — headlines say "Man Industries", never
# "MANINDS"; "Everest Kanto Cylinder", never "EKC"; "OLA Electric", never
# "OLAELEC"; "Raymond Realty", never "RAYMONDREL"; and Fujiyama Power
# Systems still trades under its pre-rename ticker UTLSOLAR, which shares
# no word with either name at all. None of this is a feed-coverage or
# bot-blocking problem (the ET-feed-retirement class of bug this file has
# hit before) — the news exists and is being fetched, it's the
# name→symbol mapping step that's blind to it. A regex over ALL-CAPS
# tokens can never solve this in general (real financial headlines are
# title-case prose, not ticker soup), so this adds a small, explicit
# alias table of name-fragments for symbols known to have this gap,
# checked as a second pass (substring match) after the direct
# ticker-token match fails. Every value here is intentionally the
# *company name*, not a ticker guess, so a false match can only ever
# resolve to the correct real symbol (still gated by known_symbols, same
# as the primary path) — extend this table whenever a new "genuine news
# exists but never got picked up" report names another mismatched ticker.
_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "MANINDS":     ("MAN INDUSTRIES",),
    "EKC":         ("EVEREST KANTO",),
    "OLAELEC":     ("OLA ELECTRIC",),
    "RAYMONDREL":  ("RAYMOND REALTY",),
    "UTLSOLAR":    ("FUJIYAMA", "UTL SOLAR"),
    # 2026-10-05 (group 151): OIL and ACE are real tickers (Oil India, Action
    # Construction Equipment) but also ordinary words, so they resolve ONLY by
    # company name (see _NAME_ONLY_TICKERS).
    "OIL":         ("OIL INDIA",),
    "ACE":         ("ACTION CONSTRUCTION",),
}

# 2026-10-05 (group 151): tickers that are ALSO everyday headline words, so
# a bare token match is almost always a false positive. The 2026-10-05 scan
# kept ['LENSKART','BPCL','CLEANMAX','WIPRO','OIL','TCS','IT','ACE'] - "IT"
# (the sector: "IT stocks rally"), "OIL" ("oil prices") and "ACE" are words,
# not the companies. Before this the known-universe path accepted ANY token
# found in the symbol master and ignored _STOPWORDS entirely, and it returned
# the first such token, so "IT stocks: TCS jumps" resolved to IT, not TCS.
#   * _GENERIC_WORD_TICKERS: sector/index/generic words - never a match; the
#     scan moves on to the next token in the headline.
#   * _NAME_ONLY_TICKERS: real companies whose ticker is an English word - the
#     bare token never matches, only the company name in _NAME_ALIASES does.
_GENERIC_WORD_TICKERS = frozenset({
    "IT", "ENERGY", "SILVER", "GOLD", "BSE", "NSE", "DIVIDEND", "CRISIL", "INDIA",
    "BANK", "AUTO", "PHARMA", "METAL", "REALTY", "FMCG", "NIFTY", "SENSEX",
    "RESULTS", "STOCKS", "SHARES",
})
_NAME_ONLY_TICKERS = frozenset({"OIL", "ACE"})


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

    After the direct token match fails, also checks _NAME_ALIASES — see
    its docstring above for why a ticker-token-only match structurally
    misses real news for any stock whose ticker isn't a word in its own
    company name.
    """
    uheadline = headline.upper()
    tokens = _SYMBOL_RE.findall(uheadline)

    if known_symbols:
        for t in tokens:
            if t in _GENERIC_WORD_TICKERS or t in _NAME_ONLY_TICKERS:
                continue  # a word, not the company - see _GENERIC_WORD_TICKERS
            if t in known_symbols:
                return t
        for sym, aliases in _NAME_ALIASES.items():
            if sym in known_symbols and any(a in uheadline for a in aliases):
                return sym
        return None

    # Degraded fallback — symbol master unavailable this pass.
    for t in tokens:
        if t in _KNOWN_NSE_SYMBOLS:
            return t
    for sym, aliases in _NAME_ALIASES.items():
        if any(a in uheadline for a in aliases):
            return sym
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


# group195: BusinessStandard's RSS answered HTTP 200 with a real <?xml ...?><rss> document, yet strict
# ElementTree refused it ("not well-formed (invalid token): line 269, column 51") — the usual cause is an unescaped
# '&' or a stray control character inside one headline/URL — and the whole feed was thrown away as "non-XML". Parsing
# now tries, in order: strict XML; XML after removing characters XML forbids and escaping bare '&'; and finally a
# plain <item>/<entry> extraction. Only a body that yields no items at all (HTML interstitial, empty page) is still
# reported as non-XML.
_XML_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_ITEM_BLOCK = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.S | re.I)


def _tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.S | re.I)
    if not m:
        return ""
    raw = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", m.group(1), flags=re.S)
    return _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _regex_feed_items(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _ITEM_BLOCK.finditer(text or ""):
        block = m.group(2)
        title = _tag_text(block, "title")
        link = _tag_text(block, "link")
        if not link:
            lm = re.search(r"<link\b[^>]*href=[\"']([^\"']+)[\"']", block, re.I)
            link = _html.unescape(lm.group(1)).strip() if lm else ""
        pub = _tag_text(block, "pubDate") or _tag_text(block, "published") or _tag_text(block, "updated")
        if title:
            out.append({"title": title, "link": link, "pubDate": pub})
    return out


def _parse_feed_text(text: str) -> tuple[list[dict], str]:
    """(items, how) — how is 'strict', 'repaired' or 'regex'. Raises ET.ParseError only when nothing at all
    could be read from the body."""
    body = (text or "").lstrip("\ufeff \t\r\n")
    try:
        return _parse_feed_items(ET.fromstring(body)), "strict"
    except ET.ParseError as first:
        err = first
    try:
        fixed = _BARE_AMP.sub("&amp;", _XML_BAD_CHARS.sub("", body))
        items = _parse_feed_items(ET.fromstring(fixed))
        if items:
            return items, "repaired"
    except ET.ParseError:
        pass
    items = _regex_feed_items(body)
    if items:
        return items, "regex"
    raise err


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
    "Accept-Language": "en-IN,en;q=0.9",
}

# 2026-10-08 (group 238): second header profile, tried on the same URL when the first answers with a
# bot-gate status. A different browser family + fuller browser-like headers is what Akamai-style gates
# look at first. Costs nothing when the first profile works.
_RSS_ALT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# HTTP statuses that mean "we are being gated", not "the feed is broken".
_BLOCK_STATUSES = frozenset({401, 403, 406, 429, 451})

# source -> time.monotonic() until which a fully-blocked feed is skipped (no point hammering a gate every
# intraday tick; the next try after the cool-down is cheap).
_FEED_BLOCKED_UNTIL: dict[str, float] = {}


def _blocked_cooldown_seconds() -> int:
    try:
        return max(60, int(getattr(config, "AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS", 1800)))
    except (TypeError, ValueError):
        return 1800


def _reset_feed_block_state() -> None:
    _FEED_BLOCKED_UNTIL.clear()


def _strip_publisher_suffix(title: str, source: str) -> str:
    """Google News titles read 'Headline - Moneycontrol'; drop the trailing publisher so symbol
    extraction and scoring see the same text the native feed would give."""
    if not title or not source:
        return title
    return re.sub(r"\s+[-\u2013|]\s+" + re.escape(source) + r"\s*$", "", title, flags=re.I).strip() or title


def _short_url(url: str) -> str:
    return url if len(url) <= 80 else url[:77] + "..."


async def _fetch_one_url(feed: dict, url: str) -> tuple[Optional[list[dict]], str]:
    """Fetch+parse ONE url. -> (items, reason). items is None when this URL failed; reason then says why
    ('HTTP 403', 'non-XML content', exception name...). A bot-gate status retries once with the alternate
    header profile before giving up on the URL."""
    reason = "unknown"
    for headers in (_RSS_FETCH_HEADERS, _RSS_ALT_HEADERS):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
                resp = await client.get(url)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as he:
                    code = he.response.status_code
                    reason = f"HTTP {code}"
                    if code in _BLOCK_STATUSES:
                        continue            # try the other header profile
                    return None, reason
                try:
                    items, how = _parse_feed_text(resp.text)
                except ET.ParseError as pe:
                    # 2026-09-17 (session58): a 200 that is not XML is a bot interstitial / retired URL,
                    # not a transient glitch - log enough of the body to tell at a glance.
                    snippet = resp.text[:120].replace("\n", " ")
                    logger.warning(
                        "afterhours-scan: %s returned non-XML content (HTTP %d): %s - body starts: %r",
                        feed["source"], resp.status_code, pe, snippet,
                    )
                    return None, "non-XML content"
                if how != "strict":
                    logger.info("afterhours-scan: %s was not strictly well-formed; read %d item(s) by %s parsing",
                                feed["source"], len(items), how)
                return items, "ok"
        except Exception as e:
            logger.warning("afterhours-scan: RSS fetch failed for %s: %s", feed["source"], e)
            return None, type(e).__name__
    return None, reason


async def _fetch_rss_items(feed: dict) -> list[dict]:
    """Fetch one RSS/Atom feed. Returns [] on any error - never crashes the scan.

    2026-10-08 (group 238, Moneycontrol HTTP 403): tries feed['url'], then each feed.get('fallback_urls')
    in order, stopping at the first that yields a readable feed. If every URL fails with a bot-gate status
    the feed is skipped for AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS (default 1800) so the 15-minute
    intraday poll does not hammer a gate; any other failure is retried on the next tick as before."""
    source = feed["source"]
    now = time.monotonic()
    until = _FEED_BLOCKED_UNTIL.get(source, 0.0)
    if until > now:
        logger.info("afterhours-scan: %s skipped - blocked, retry in %d s", source, int(until - now))
        return []

    urls = [feed["url"], *[u for u in (feed.get("fallback_urls") or []) if u]]
    reasons: list[str] = []
    for idx, url in enumerate(urls):
        items, reason = await _fetch_one_url(feed, url)
        if items is not None:
            if idx > 0:
                items = [dict(it, title=_strip_publisher_suffix(it.get("title", ""), source)) for it in items]
                logger.info("afterhours-scan: %s primary feed unavailable (%s) - served by fallback %s: %d item(s)",
                            source, "; ".join(reasons) or "failed", _short_url(url), len(items))
            _FEED_BLOCKED_UNTIL.pop(source, None)
            logger.debug("afterhours-scan: %s -> %d items", source, len(items))
            return items
        reasons.append(f"{_short_url(url)}: {reason}")

    if any(r.endswith(f"HTTP {c}") for r in reasons for c in _BLOCK_STATUSES):
        cool = _blocked_cooldown_seconds()
        _FEED_BLOCKED_UNTIL[source] = time.monotonic() + cool
        logger.warning("afterhours-scan: %s blocked on all %d URL(s) (%s) - skipping this feed for %d s",
                       source, len(urls), " | ".join(reasons), cool)
    elif len(urls) > 1:
        logger.warning("afterhours-scan: %s failed on all %d URL(s): %s", source, len(urls), " | ".join(reasons))
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

def _format_funnel(funnel: list[dict], bulk_count: int) -> str:
    """One-line per-feed funnel: items/stale/no-symbol/score<=0/scored, plus bulk-deal hits."""
    parts = [
        f"{f['source']}: {f['items']} items, {f['stale']} stale, {f['no_symbol']} no-symbol, "
        f"{f['score0']} score<=0, {f['scored']} scored"
        for f in funnel
    ]
    parts.append(f"bulk/block: {bulk_count} hit(s)")
    return " | ".join(parts)


def _explain_empty(funnel: list[dict], bulk_count: int, have_master: bool) -> str:
    """Plain-English reason a pass found nothing to score."""
    total = sum(f["items"] for f in funnel)
    if total == 0 and bulk_count == 0:
        return "every RSS feed returned 0 items (fetch failed or blocked — see the feed warnings) and there were no bulk/block-deal hits"
    stale = sum(f["stale"] for f in funnel)
    no_sym = sum(f["no_symbol"] for f in funnel)
    zero = sum(f["score0"] for f in funnel)
    reasons = []
    if stale:
        reasons.append(f"{stale} older than the max news age")
    if no_sym:
        reasons.append(f"{no_sym} matched no NSE symbol" + ("" if have_master else " (symbol master unavailable, whitelist fallback)"))
    if zero:
        reasons.append(f"{zero} scored <= 0")
    if not reasons:
        reasons.append("none passed the filters")
    return f"{total} RSS item(s) fetched: " + ", ".join(reasons) + f"; {bulk_count} bulk/block-deal hit(s)"


# Telegram noise control (group 95, item 19). A scan runs every few minutes for both modes and on
# every service boot; it used to send a message each time, including "No new rows - N symbol(s)
# already at best score" (nothing to act on) and, after a restart, a repeat of the same list. Now a
# SCHEDULED scan messages only when it wrote something new; a manual scan always replies; a pass
# that failed to save everything is reported once per (mode, date, failed-count), not every tick.
_zero_row_notice_seen: dict = {}


def _should_notify_scan(mode: str, market_date: str, written: int, failed: int, manual: bool) -> bool:
    if manual or written:
        return True
    if failed:
        key = (mode, market_date)
        if _zero_row_notice_seen.get(key) == failed:
            return False
        _zero_row_notice_seen[key] = failed
        if len(_zero_row_notice_seen) > 64:      # bounded: keep only the newest entries
            for k in list(_zero_row_notice_seen)[:-32]:
                _zero_row_notice_seen.pop(k, None)
        return True
    return False


async def run_afterhours_scan(db, mode: str, market_date: str, manual: bool = False) -> int:
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

    # 2026-10-04 (item 17): per-feed funnel so a pass that writes 0 rows says
    # WHY (feed empty / all stale / no symbol matched / all scored <= 0 /
    # already stored at best score / upsert failed) instead of ending silently.
    funnel: list[dict] = []

    for feed in _RSS_FEEDS:
        items = await _fetch_rss_items(feed)
        stale_dropped = 0
        no_symbol = 0
        zero_score = 0
        scored = 0
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
                no_symbol += 1
                continue
            catalyst_types = classify_text(headline) or ["news"]
            score = _score_headline(headline, catalyst_types, feed["source_bonus"])
            if score <= 0:
                zero_score += 1
                continue
            scored += 1
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
        funnel.append({
            "source": feed["source"], "items": len(items), "stale": stale_dropped,
            "no_symbol": no_symbol, "score0": zero_score, "scored": scored,
        })

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
    funnel_summary = _format_funnel(funnel, len(bulk_hits))
    for symbol, hit in bulk_hits.items():
        existing = best.get(symbol)
        if existing is None or hit["score"] > existing["score"]:
            best[symbol] = hit

    if not best:
        logger.info(
            "afterhours-scan [%s %s]: no scored items found this pass — 0 rows written (%s)",
            mode, market_date, _explain_empty(funnel, len(bulk_hits), bool(known_symbols)),
        )
        logger.info("afterhours-scan [%s %s]: funnel %s", mode, market_date, funnel_summary)
        # Group 97: group95 documented "a manual scan always replies", but this
        # empty-pass early return never sent anything, so the Run Now button
        # got no Telegram reply when nothing scored. Scheduled passes stay silent.
        if manual:
            try:
                from notifier import notify_async
                await notify_async(
                    f"📡 *After-hours scan — {mode}* ({market_date})\n"
                    f"No scored items this pass — 0 rows written.\n"
                    f"{_explain_empty(funnel, len(bulk_hits), bool(known_symbols))}"
                )
            except Exception:
                logger.debug("afterhours-scan: Telegram notification failed (non-fatal)", exc_info=True)
        return 0

    now = datetime.now(timezone.utc)
    written = 0
    unchanged = 0
    failed = 0

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
            else:
                unchanged += 1
        except Exception as e:
            db.rollback()
            failed += 1
            logger.warning(
                "afterhours-scan [%s %s]: failed to upsert %s — skipping: %s",
                mode, market_date, symbol, e,
            )
            continue

    # Group 121: the old line ("10 symbol(s) scored → 1 rows upserted") read as
    # if 9 symbols were lost. They were not: after a restart or a repeat pass the
    # earlier pass's rows are already stored at an equal or higher score, so only
    # genuinely new/improved symbols are written. Say so, with the full split.
    # Group 130: when nothing was written, the "0 rows written - <why>" line below already says
    # the same thing (the boot log printed both back to back), so the split line is for writes only.
    if written > 0:
        logger.info(
            "afterhours-scan [%s %s]: %d symbol(s) scored → %d new/updated row(s), "
            "%d already stored at an equal or higher score (unchanged), %d failed",
            mode, market_date, len(best), written, unchanged, failed,
        )
    if written == 0:
        # Item 17: say why nothing was written.
        if failed and failed == len(best):
            why = f"every one of {failed} upsert(s) FAILED (see the warnings above)"
        elif failed:
            why = (f"{unchanged} already stored at an equal/higher score, "
                   f"{failed} upsert(s) FAILED (see the warnings above)")
        else:
            why = (f"all {unchanged} scored symbol(s) already stored for {market_date} "
                   "at an equal or higher score (nothing new to write)")
        logger.info("afterhours-scan [%s %s]: 0 rows written — %s", mode, market_date, why)
        logger.info("afterhours-scan [%s %s]: funnel %s", mode, market_date, funnel_summary)

    # ── Telegram notification (2026-09-18 fix, session67) ────────────────────
    # Notify on every scan tick (auto or manual) with what was found/updated.
    # Best-effort — a Telegram failure must never fail the scan.
    if not _should_notify_scan(mode, market_date, written, failed, manual):
        logger.info("afterhours-scan [%s %s]: no Telegram message (nothing new to report)", mode, market_date)
        return written
    try:
        from notifier import notify_async
        # Sort by score descending for the notification summary
        sorted_hits = sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True)
        lines = [f"📡 *After-hours scan — {mode}* ({market_date})"]
        if written:
            kept = f" · {unchanged} already stored" if unchanged else ""
            lines.append(f"{written} row(s) new/updated · {len(best)} total scored this pass{kept}\n")
        else:
            if failed:
                lines.append(f"No new rows — {unchanged} symbol(s) already at best score, {failed} failed to save\n")
            else:
                lines.append(f"No new rows — {len(best)} symbol(s) already at best score\n")
        for sym, hit in sorted_hits[:_NOTIFY_MAX_SYMBOLS]:  # was top 8 (2026-10-04: show all, cap only as a sanity bound)
            cat_icon = {"results": "📊", "bulk_block": "🏦", "board": "🗂️", "insider": "👤", "news": "📰"}.get(
                hit["catalyst_type"], "📰"
            )
            lines.append(
                f"{cat_icon} *{sym}* · score {hit['score']:.0f} · {hit['catalyst_type']} · {hit['source']}"
            )
            # 2026-10-04: full headline (was cut at 80 chars + '…'). Telegram's 4096
            # limit is handled by the notification service, which splits long
            # messages into numbered parts, so no truncation is needed here.
            hl = hit.get("headline", "")
            if hl:
                lines.append(f"   _{hl[:_NOTIFY_MAX_HEADLINE]}_")
        if len(sorted_hits) > _NOTIFY_MAX_SYMBOLS:
            lines.append(f"\n…and {len(sorted_hits) - _NOTIFY_MAX_SYMBOLS} more not shown")
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
                lines.append(f"   _{r.headline[:_NOTIFY_MAX_HEADLINE]}_")
        lines.append("\n_These will be injected as overnight-priority candidates at 09:00 IST._")
        await notify_async("\n".join(lines))
    except Exception:
        logger.debug("afterhours-scan finalize: Telegram notification failed (non-fatal)", exc_info=True)

    return shortlist