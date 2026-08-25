"""
Shared symbol-rename / delisted-symbol table for api-gateway.

Why this exists: market-data-service already has SMART_SYMBOL_MAP and
normalize_symbol() (see services/market-data-service/main.py) so its
/quotes/bulk and /quote/{symbol} endpoints correctly turn ZOMATO -> ETERNAL
before ever calling yfinance. But api-gateway's main.py and ipo_scanner.py
have ~9 call sites that build f"{sym}.NS" directly and call yf.Ticker(...)
themselves (Hot Picks, market movers, IPO history fallback, RSI repair,
etc.) — those never went through market-data-service's map, which is why
logs show:

    ERROR:yfinance:$ZOMATO.NS: possibly delisted; no price data found
    ERROR:yfinance:HTTP Error 404 ... Quote not found for symbol: CISCO.NS

ZOMATO is not delisted — it renamed to ETERNAL on 2025-04-09 (same company,
same app, new ticker on both NSE and BSE). CISCO is not an NSE symbol at
all (US-listed, Nasdaq: CSCO) and was never going to resolve with ".NS"
appended — that one belongs on a permanent skip-list, not a retry loop.

This module now handles THREE separate failure modes a bare "sym + .NS"
call site can hit:
  1. Known rename (SYMBOL_RENAMES, static)          -> resolved silently
  2. Non-NSE symbol (KNOWN_NOT_ON_NSE, static)       -> None, skip outright
  3. Unknown rename / newly-delisted symbol we don't have on file yet
     -> resolve_with_fallback() learns it durably (see "Dynamic durable
        rename learning" below) instead of retrying the same dead symbol
        every single cycle forever.
  4. Known chronically-over-₹5000 symbol (KNOWN_HIGH_PRICE_SYMBOLS)
     -> flagged via is_known_high_price() so callers can skip the network
        round-trip entirely instead of fetching a quote just to find out
        what a static list already tells us.

Usage (existing call sites — unchanged):
    from symbol_aliases import resolve_ns_ticker
    ticker = resolve_ns_ticker(sym)   # None => don't call yfinance at all
    if ticker:
        yf.Ticker(ticker)...

Usage (new — for a call site that just got a 404/delisted error and wants
to try to recover automatically instead of giving up):
    from symbol_aliases import resolve_with_fallback
    ticker, info = resolve_with_fallback(sym)  # tries dynamic NSE lookup
                                                # once, learns + caches result
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("symbol-aliases")

# Company renames / ticker changes on NSE. Keep in sync with
# services/market-data-service/main.py's SMART_SYMBOL_MAP — that file is
# the source of truth for anything with ambiguous multi-word names; this
# one only needs the *rename* entries since every call site here already
# has a clean bare symbol (no spaces) by the time it gets here.
SYMBOL_RENAMES = {
    "ZOMATO": "ETERNAL",       # 2025-04-09: Zomato Ltd -> Eternal Ltd (NSE + BSE)
    "MINDTREE": "LTIM",        # merged into L&T Infotech -> LTIMindtree
    "SRTRANSFIN": "SHRIRAMFIN",
    "GMRINFRA": "GMRAIRPORT",
    "MOTHERSUMI": "MOTHERSON",
    "CADILAHC": "ZYDUSLIFE",
    "PVR": "PVRINOX",
    "IBULHSGFIN": "SAMMAANCAP",
    # Additional NSE renames/mergers, added while durability-hardening this
    "L&TFH": "LTF",                 # L&T Finance Holdings -> LTF
    "ADANITRANS": "ADANIENSOL",     # demerger -> Adani Energy Solutions
    "SBILIFE": "SBILIFE",           # unchanged, present so alias table stays a superset
    "JETAIRWAYS": "JETAIRWAYS",     # kept explicit — DO NOT auto-map to KNOWN_NOT_ON_NSE
    "NSPIRA": "NSIL",               # placeholder pattern — replace as real changes surface
    # ── Reconciled with market-data-service/main.py:SMART_SYMBOL_MAP ──────────
    # Compact, space-stripped company forms that arrive from search boxes and
    # news text. market-data-service already normalised these; this table did
    # not, so the same input resolved differently depending on which service saw
    # it first. Neither file disagreed on a target — these were simply absent.
    "KFINTECHNOLOGIES": "KFINTECH",
    "KPITTECHNOLOGIES": "KPITTECH",
    "ONE97": "PAYTM",               # One97 Communications -> trades as PAYTM
    # ── Added 2026-08-24 (Database Feed Health repair-loop audit) ──────────
    # "JUBILANT" is not, and never was, a real NSE ticker — it was a bad/
    # truncated entry sitting alongside the correct "JUBLFOOD" in main.py's
    # static universe fallback list. yfinance correctly reports it
    # "possibly delisted" every single repair cycle because there is
    # nothing to find. Map it to the real ticker (Jubilant FoodWorks)
    # instead of leaving it to burn a network call and fail forever.
    "JUBILANT": "JUBLFOOD",
}

# Symbols that are not NSE-listed at all (foreign tickers, indices sent by
# mistake, etc.) — appending ".NS" and calling yfinance for these just
# burns rate-limit budget on a request that can never succeed. Skip them
# outright instead of retrying every cycle.
KNOWN_NOT_ON_NSE = {
    "CISCO", "CSCO", "GOOGL", "GOOG", "AAPL", "MSFT", "AMZN", "META", "TSLA",
    "NVDA", "NFLX", "INTC", "AMD", "IBM", "ORCL",
}

# Genuinely delisted/merged-away symbols — NOT a rename. A rename means
# "same company, new ticker, 1:1" (safe to substitute in resolve_ns_ticker).
# These are cap-structure mergers/cancellations where the old ticker simply
# stops existing and converts into a DIFFERENT share count of another
# ticker (a straight symbol swap here would silently mis-price/mis-quote
# the surviving instrument), so they get their own skip-list instead of
# living in SYMBOL_RENAMES.
KNOWN_DELISTED = {
    # Tata Motors 'A' Ordinary (DVR) shares — suspended from trading
    # 2024-08-30 under a Scheme of Arrangement; holders received 7 ordinary
    # TATAMOTORS shares for every 10 TATAMTRDVR shares held (not a 1:1
    # rename). Confirmed via NSE/Zerodha bulletin. Kept out of
    # SYMBOL_RENAMES on purpose — see module note above.
    "TATAMTRDVR": "merged into TATAMOTORS 2024-08-30 (7:10 ratio, not 1:1)",
}


def is_known_delisted(symbol: str) -> bool:
    """True when `symbol` is a confirmed genuine delisting/merger (not a
    simple rename) — callers should stop tracking/repairing it and purge
    any stale feed row instead of retrying forever."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    return base in KNOWN_DELISTED

# Chronically-over-₹5000 NSE names. This is deliberately a SHORT, high-
# confidence static list (not an attempt to track every high-priced stock
# on NSE) — its only job is to let the ≤₹5000 universe/data-feed gate (see
# main.py MAX_UNIVERSE_PRICE) skip the live-quote round trip for names that
# are essentially never going to legitimately re-enter the universe, so
# they stop showing up as "price: missing" in Database Feed Health forever
# without ever getting purged (that purge previously only fired *after* a
# live quote succeeded — under rate-limiting that quote often never came
# back, so the symbol just sat there being retried every refill cycle).
# A symbol here is skipped/purged immediately, no network call needed.
KNOWN_HIGH_PRICE_SYMBOLS = {
    "MRF", "PAGEIND", "HONAUTO", "SHREECEM", "3MINDIA", "BOSCHLTD",
    "MARUTI", "ELGIEQUIP", "ABBOTINDIA", "NESCO",
    # Added from repair-loop logs (2026-08-24 audit): these were NOT on the
    # static list, so every repair cycle burned a live /quote/{symbol} call
    # to rediscover "yep, still over cap" and then purge the row — forever,
    # every cycle, for the same symbols. Confirmed >₹5000 as of the log's
    # observed quotes (KEI ₹5527.60, LINDEINDIA ₹6672.00, NAVINFLUOR
    # ₹8206.50, NETWEB ₹5601.00, NEULANDLAB ₹23301.00, PERSISTENT ₹5667.50,
    # POWERINDIA ₹34190.00, PTCIL ₹20629.00, SOLARINDS ₹19900.00,
    # ULTRACEMCO ₹11570.00, APARINDS ₹16856.00, BAJAJ-AUTO ₹11700.00,
    # POLYCAB ₹8966.00).
    "KEI", "LINDEINDIA", "NAVINFLUOR", "NETWEB", "NEULANDLAB", "PERSISTENT",
    "POWERINDIA", "PTCIL", "SOLARINDS", "ULTRACEMCO", "APARINDS",
    "BAJAJ-AUTO", "POLYCAB",
    # ABB and APOLLOHOSP are not in the pasted log's purge lines, but are
    # well-established chronically->₹5000 NSE names (ABB India routinely
    # trades ₹6,000-9,000+; Apollo Hospitals routinely ₹6,000-7,500+) and
    # were also flagged as "missing" (never-populated) rows in the Database
    # Feed Health screenshot — same failure mode, listed here so repair
    # stops retrying them too.
    "ABB", "APOLLOHOSP",
    # Cross-checked against multiple independent Aug 2026 "most expensive
    # NSE/BSE shares" sources (Business Standard's Nifty-500 >₹10,000 list,
    # Tickertape, Samco): each of these is corroborated as chronically
    # trading many multiples of the ₹5000 cap, with no recent stock split
    # on record that would have brought it back under the cap.
    "ELCID",     # Elcid Investments — ~₹1.24 lakh/share
    "GILLETTE",  # Gillette India — routinely ₹8,000-11,000
    "OFSS",      # Oracle Financial Services Software — ~₹12,000+
    "JSWHL",     # JSW Holdings — routinely ₹8,000+, very low float
}


def is_known_high_price(symbol: str) -> bool:
    """True when `symbol` is on the static chronically->₹5000 list — callers
    can use this to skip a live-quote fetch entirely instead of burning a
    network call to (re)discover the same fact every cycle."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    return base in KNOWN_HIGH_PRICE_SYMBOLS


def resolve_ns_ticker(symbol: str) -> Optional[str]:
    """
    Turn a bare NSE symbol into the correct 'XXXX.NS' Yahoo ticker,
    applying known renames (static table + any durably-learned renames).
    Returns None when the symbol is a known non-NSE / delisted name that
    should not be sent to yfinance at all.
    """
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None
    if base in KNOWN_NOT_ON_NSE or base in KNOWN_DELISTED:
        return None
    base = _apply_all_renames(base)
    return f"{base}.NS"


def resolve_base_symbol(symbol: str) -> Optional[str]:
    """Same resolution as resolve_ns_ticker but returns the bare symbol
    (no .NS suffix), or None if it's a known non-NSE / genuinely-delisted
    name."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None
    if base in KNOWN_NOT_ON_NSE or base in KNOWN_DELISTED:
        return None
    return _apply_all_renames(base)


def _apply_all_renames(base: str) -> str:
    learned = _load_learned_renames()
    entry = learned.get(base)
    if isinstance(entry, dict) and entry.get("to"):
        base = entry["to"]
    return SYMBOL_RENAMES.get(base, base)


# ── Dynamic durable rename learning ─────────────────────────────────────────
# When a call site's quote/history fetch comes back "delisted"/"not found"
# for a symbol NOT already in SYMBOL_RENAMES or KNOWN_NOT_ON_NSE, that's
# either (a) a genuine rename we don't have on file yet, or (b) a real
# delisting. We can't tell which from a single 404, so the flow is:
#
#   1. record_resolution_failure(symbol) — bump a durable failure counter.
#   2. try_discover_rename(symbol) — best-effort live NSE lookup (NSE's
#      corporate-announcements feed frequently carries a "change of
#      symbol"/"change of name" notice for genuine renames). If it finds a
#      confident new symbol, learn_rename() persists it immediately and
#      every future resolve_ns_ticker() call picks it up with zero extra
#      network calls — this is what makes the fix durable across restarts
#      instead of being an in-memory-only patch.
#   3. If discovery finds nothing after MAX_FAILURE_STREAK consecutive
#      failures, the symbol is durably marked as "probably delisted" (a
#      separate learned-negative cache) so callers stop retrying it via
#      the network every cycle — same intent as KNOWN_NOT_ON_NSE, just
#      learned at runtime instead of hardcoded.
#
# All of this is opportunistic/best-effort: NSE's endpoints are unofficial
# and frequently block cloud IPs, so a "no discovery" result just means we
# fall back to treating the symbol as still-unresolved (existing behaviour,
# no regression) — it never blocks or slows down the caller.

_LEARNED_RENAMES_KEY = "stockky:known_symbols:learned_renames"
_LEARNED_DELISTED_KEY = "stockky:known_symbols:learned_delisted"
_FAILURE_COUNTS_KEY = "stockky:known_symbols:resolution_failures"
MAX_FAILURE_STREAK = 5


def _kv():
    import kv_cache
    return kv_cache


def _load_learned_renames() -> Dict[str, Any]:
    try:
        val = _kv().kv_get(_LEARNED_RENAMES_KEY)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _load_learned_delisted() -> Dict[str, Any]:
    try:
        val = _kv().kv_get(_LEARNED_DELISTED_KEY)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def is_learned_delisted(symbol: str) -> bool:
    """True when we've previously tried (and failed, repeatedly) to resolve
    this symbol dynamically and concluded it's probably genuinely delisted
    — not just a rename we haven't learned yet. Callers can treat this the
    same as KNOWN_NOT_ON_NSE (skip the network call outright)."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    return base in _load_learned_delisted()


def learn_rename(old_symbol: str, new_symbol: str, source: str = "manual") -> None:
    """Durably record that old_symbol now trades as new_symbol. Call this
    from anywhere that discovers a rename — a manual admin action, a
    successful try_discover_rename() result, or a hardcoded patch applied
    at runtime instead of via a deploy."""
    old = (old_symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    new = (new_symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not old or not new or old == new:
        return
    try:
        data = _load_learned_renames()
        data[old] = {
            "to": new,
            "source": source,
            "learned_at": datetime.now(timezone.utc).isoformat(),
        }
        _kv().kv_set(_LEARNED_RENAMES_KEY, data, ttl=None)  # durable, no expiry
        logger.info("symbol_aliases: learned rename %s -> %s (source=%s)", old, new, source)
        # A confirmed rename supersedes any earlier "probably delisted" guess.
        try:
            delisted = _load_learned_delisted()
            if old in delisted:
                delisted.pop(old, None)
                _kv().kv_set(_LEARNED_DELISTED_KEY, delisted, ttl=None)
        except Exception:
            pass
    except Exception as e:
        logger.warning("symbol_aliases: could not persist learned rename %s->%s: %s", old, new, e)


def record_resolution_failure(symbol: str) -> int:
    """Bump the durable failure streak for `symbol`; returns the new count.
    Once MAX_FAILURE_STREAK is hit, is_learned_delisted() starts returning
    True for it so callers can stop trying."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return 0
    try:
        counts = _kv().kv_get(_FAILURE_COUNTS_KEY)
        counts = counts if isinstance(counts, dict) else {}
        n = int(counts.get(base, 0)) + 1
        counts[base] = n
        _kv().kv_set(_FAILURE_COUNTS_KEY, counts, ttl=30 * 86400)
        if n >= MAX_FAILURE_STREAK:
            delisted = _load_learned_delisted()
            delisted[base] = {"marked_at": datetime.now(timezone.utc).isoformat(), "failures": n}
            _kv().kv_set(_LEARNED_DELISTED_KEY, delisted, ttl=None)
            logger.info(
                "symbol_aliases: %s hit %s consecutive resolution failures — "
                "marked probably-delisted (learned, not hardcoded)", base, n,
            )
        return n
    except Exception:
        return 0


def clear_resolution_failures(symbol: str) -> None:
    """Reset the failure streak once a symbol resolves successfully again."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    try:
        counts = _kv().kv_get(_FAILURE_COUNTS_KEY)
        if isinstance(counts, dict) and base in counts:
            counts.pop(base, None)
            _kv().kv_set(_FAILURE_COUNTS_KEY, counts, ttl=30 * 86400)
    except Exception:
        pass


_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}

_RENAME_SUBJECT_PATTERNS = (
    re.compile(r"CHANGE\s+OF\s+(?:TRADING\s+)?SYMBOL[^A-Z0-9]{0,15}(?:TO|FROM.*TO)?\s*([A-Z0-9&\-]{2,20})"),
    re.compile(r"NEW\s+SYMBOL\s*[:\-]?\s*([A-Z0-9&\-]{2,20})"),
    re.compile(r"REVISED\s+SYMBOL\s*[:\-]?\s*([A-Z0-9&\-]{2,20})"),
)


def try_discover_rename(old_symbol: str, timeout: float = 10.0) -> Optional[str]:
    """
    Best-effort dynamic fallback for a symbol that failed to resolve and
    isn't in the static SYMBOL_RENAMES table: scan NSE's unofficial
    corporate-announcements feed for a "change of symbol"/"new symbol"
    notice naming the replacement ticker.

    This is intentionally best-effort — NSE blocks a meaningful fraction of
    cloud-hosted IPs even with a correct cookie handshake, so a None return
    here just means "couldn't confirm a rename right now", not "definitely
    delisted" (that's what record_resolution_failure's streak is for).
    On a confident hit this ALSO calls learn_rename() so the result is
    durable and this network call never needs to run again for this symbol.
    """
    base = (old_symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base or base in KNOWN_NOT_ON_NSE:
        return None
    try:
        import httpx
    except Exception:
        return None
    try:
        with httpx.Client(timeout=timeout, headers=_NSE_HEADERS, follow_redirects=True) as c:
            try:
                c.get("https://www.nseindia.com", timeout=timeout)
            except Exception:
                pass
            r = c.get(
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "symbol": base},
                timeout=timeout,
            )
            if r.status_code != 200:
                logger.info("try_discover_rename(%s): NSE announcements HTTP %s", base, r.status_code)
                return None
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("data") or [])
    except Exception as e:
        logger.info("try_discover_rename(%s) failed: %s", base, e)
        return None

    for row in rows:
        subj = f"{row.get('subject') or row.get('desc') or ''} {row.get('attachment') or ''}".upper()
        if "CHANGE" not in subj and "REVISED" not in subj and "NEW SYMBOL" not in subj:
            continue
        if "SYMBOL" not in subj and "NAME" not in subj:
            continue
        for pattern in _RENAME_SUBJECT_PATTERNS:
            m = pattern.search(subj)
            if m:
                new_symbol = m.group(1).strip().strip("-&")
                if new_symbol and new_symbol != base and new_symbol not in KNOWN_NOT_ON_NSE:
                    learn_rename(base, new_symbol, source="nse_corp_announcements")
                    return new_symbol
    return None


def resolve_with_fallback(symbol: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Full recovery path for a call site that just hit a 404/"possibly
    delisted" error on `symbol` and wants to try to self-heal instead of
    giving up: static table -> learned rename -> (if still unresolved) a
    live NSE discovery attempt -> learned-delisted short-circuit.

    Returns (ticker_or_None, info) where info explains what happened:
      {"resolution": "static_rename" | "learned_rename" | "unchanged"
                    | "discovered_rename" | "skip_not_nse" | "skip_delisted"
                    | "unresolved"}
    Only call this from an actual failure-recovery branch (it can make a
    live network call) — normal-path resolution should keep using the plain
    resolve_ns_ticker() above.
    """
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None, {"resolution": "empty"}
    if base in KNOWN_NOT_ON_NSE:
        return None, {"resolution": "skip_not_nse"}
    if base in KNOWN_DELISTED:
        return None, {"resolution": "skip_delisted_merged", "detail": KNOWN_DELISTED[base]}
    if is_learned_delisted(base):
        return None, {"resolution": "skip_delisted"}

    if base in SYMBOL_RENAMES:
        target = SYMBOL_RENAMES[base]
        return f"{target}.NS", {"resolution": "static_rename", "to": target}

    learned = _load_learned_renames()
    if base in learned and learned[base].get("to"):
        target = learned[base]["to"]
        return f"{target}.NS", {"resolution": "learned_rename", "to": target}

    discovered = try_discover_rename(base)
    if discovered:
        return f"{discovered}.NS", {"resolution": "discovered_rename", "to": discovered}

    n = record_resolution_failure(base)
    return f"{base}.NS", {"resolution": "unresolved", "failure_streak": n}
