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

Usage:
    from symbol_aliases import resolve_ns_ticker
    ticker = resolve_ns_ticker(sym)   # None => don't call yfinance at all
    if ticker:
        yf.Ticker(ticker)...
"""
from __future__ import annotations

from typing import Optional

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
}

# Symbols that are not NSE-listed at all (foreign tickers, indices sent by
# mistake, etc.) — appending ".NS" and calling yfinance for these just
# burns rate-limit budget on a request that can never succeed. Skip them
# outright instead of retrying every cycle.
KNOWN_NOT_ON_NSE = {
    "CISCO", "CSCO", "GOOGL", "GOOG", "AAPL", "MSFT", "AMZN", "META", "TSLA",
    "NVDA", "NFLX", "INTC", "AMD", "IBM", "ORCL",
}


def resolve_ns_ticker(symbol: str) -> Optional[str]:
    """
    Turn a bare NSE symbol into the correct 'XXXX.NS' Yahoo ticker,
    applying known renames. Returns None when the symbol is a known
    non-NSE / delisted name that should not be sent to yfinance at all.
    """
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None
    if base in KNOWN_NOT_ON_NSE:
        return None
    base = SYMBOL_RENAMES.get(base, base)
    return f"{base}.NS"


def resolve_base_symbol(symbol: str) -> Optional[str]:
    """Same resolution as resolve_ns_ticker but returns the bare symbol
    (no .NS suffix), or None if it's a known non-NSE name."""
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not base:
        return None
    if base in KNOWN_NOT_ON_NSE:
        return None
    return SYMBOL_RENAMES.get(base, base)
