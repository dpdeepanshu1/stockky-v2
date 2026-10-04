"""
Peer-relative metrics + Multi-quarter consistency (free-tier).

Uses data already available from market-data-service / yfinance-style fundamentals.
No paid APIs required.

Peer relative:
  - Compare stock PE, ROE, growth vs a small sector peer set
  - Returns relative scores useful for fundamental score and prediction features

Multi-quarter:
  - Check last 2–3 quarters of revenue / profit growth consistency
  - Flag stable positive growth vs one-off spikes
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("peer_multi_quarter")

# ── Fix (30 Aug 2026): peer-fundamentals fetch was the root cause of
# /fundamental/analyze/{symbol} consistently taking ~12s (and, downstream,
# api-gateway's /scan/watchlist consistently timing out at the test
# harness's 25s curl limit — see FIXES_30AUG2026_TEST_FAILURES.md).
#
# A single analyze() call ended up calling this module's fetch for peer
# fundamentals from FOUR separate places (the inline peer loop in
# fundamental/main.py, rank_against_peers()'s own ranking loop,
# compute_peer_relative() called from inside rank_against_peers(), and
# compute_peer_relative() called again from inside
# enrich_fundamentals_with_peer_and_consistency()) — each doing its own
# SEQUENTIAL for-loop of blocking HTTP calls, so a symbol's peers could be
# fetched over the network up to 4x each, one at a time.
#
# Fix has two parts:
#   1. A short-TTL in-process cache keyed by symbol, shared by every
#      call-site in this file (and by peer_ranking.py, which imports
#      fetch_fundamentals from here) — repeat lookups for the same peer
#      within the TTL window are free instead of a fresh network round trip.
#   2. fetch_fundamentals_batch() fetches any still-uncached symbols in
#      parallel via a small thread pool, so a cold-cache lookup across N
#      peers costs roughly one round trip instead of N sequential ones.
# Behavior/return shape of fetch_fundamentals() and compute_peer_relative()
# is unchanged — only how the data underneath is obtained.
_FUND_CACHE: Dict[str, tuple] = {}  # symbol -> (fetched_at_epoch, data)
_FUND_CACHE_LOCK = threading.Lock()
_FUND_CACHE_TTL = float(((os.getenv("PEER_FUNDAMENTALS_CACHE_TTL_SECONDS") or "").strip() or "60"))
_FUND_FETCH_MAX_WORKERS = int(((os.getenv("PEER_FUNDAMENTALS_FETCH_WORKERS") or "").strip() or "6"))

# Single default for "how many peers to compare" - rank_against_peers() and
# compute_peer_relative() used to default to 6 and 5, so the same call could
# rank against one peer set and score against another.
DEFAULT_MAX_PEERS = 5

# Default peer maps for common Indian sectors (extend as needed)
DEFAULT_PEERS: Dict[str, List[str]] = {
    "IT": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
    "BANK": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS"],
    "AUTO": ["MARUTI.NS", "TATAMOTORS.NS", "M&M.NS", "BAJAJ-AUTO.NS", "HEROMOTOCO.NS"],
    "PHARMA": ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "APOLLOHOSP.NS"],
    "FMCG": ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS"],
    "METAL": ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS", "COALINDIA.NS"],
    "ENERGY": ["RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "GAIL.NS"],
    # 2026-10-04 (log-audit item 10): utilities / renewables (Yahoo "Utilities") used to be read as "IT"
    # because the substring "IT" sits inside "UTILITIES"; and retailers (Yahoo "...Retail") had no peer
    # set at all and fell through to the generic large-cap DEFAULT list below.
    "POWER": ["NTPC.NS", "POWERGRID.NS", "TATAPOWER.NS", "ADANIPOWER.NS", "JSWENERGY.NS", "NHPC.NS"],
    "RETAIL": ["DMART.NS", "TRENT.NS", "ABFRL.NS", "SHOPERSTOP.NS", "VMART.NS", "BATAINDIA.NS"],
    "DEFAULT": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"],
}


def _safe(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        f = float(val)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default

_PE_KEYS = ("pe_ratio", "trailingPE", "pe")
_ROE_KEYS = ("roe", "returnOnEquity")
_REV_G_KEYS = ("revenue_growth_yoy", "revenueGrowth")
_PROFIT_G_KEYS = ("profit_growth_yoy", "earningsGrowth")


def _pick(d: Any, keys: Tuple[str, ...], skip_zero: bool = False, default: float = 0.0) -> float:
    """First usable numeric value among `keys` (primary key first, then aliases).

    Replaces the `_safe(d.get("a") or d.get("b"))` idiom, where `or` treated a
    genuine 0.0 in the primary key (e.g. 0% growth, 0% ROE) as missing and
    silently read the alias instead - which could be a different number.
    A value is skipped only if it is absent, None, NaN or non-numeric. With
    skip_zero=True a 0 is skipped too; that is for P/E, where 0 means "not
    reported", not a real ratio.
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f or (skip_zero and f == 0):
            continue
        return f
    return default


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s.endswith(".NS") and not s.endswith(".BO"):
        s = f"{s}.NS"
    return s


def build_peer_list(
    symbol: str,
    sector: str,
    peers: Optional[List[str]] = None,
    max_peers: int = DEFAULT_MAX_PEERS,
) -> List[str]:
    """The peers to compare `symbol` against: canonical (".NS"/".BO"), de-duplicated,
    never containing the symbol itself, at most `max_peers` long (zero or negative
    -> none). Shared by rank_against_peers() and compute_peer_relative() so both
    always use the same peer set.

    Previously compute_peer_relative() kept peers exactly as given, so a bare
    "TCS" was fetched as "TCS" (a symbol market-data does not know) instead of
    "TCS.NS", spellings like "B"/"b"/"B.NS" were fetched and averaged as three
    peers, and a negative max_peers sliced from the END of the list.
    """
    symbol = _norm_symbol(symbol)
    raw = peers or DEFAULT_PEERS.get(sector, DEFAULT_PEERS["DEFAULT"])
    out: List[str] = []
    for p in raw:
        n = _norm_symbol(p)
        if n != symbol and n not in out:
            out.append(n)
    return out[: max(max_peers, 0)]


# Sector words, matched against whole WORDS of the sector/industry text (never substrings):
# the old `"IT" in text` test fired inside "CAPITAL", "UTILITIES", "HOSPITALITY"..., and the old bare
# "CONSUMER" -> FMCG sent Yahoo's whole "Consumer Cyclical" sector (autos, retail, apparel) to FMCG peers.
# Each entry is (sector key, exact words, word prefixes), checked in this order (first hit wins).
_SECTOR_WORDS = (
    ("IT", ("IT",), ("SOFTWARE", "TECH")),                      # TECH also covers TECHNOLOGY, not BIOTECHNOLOGY
    ("BANK", (), ("BANK", "FINANCIAL", "NBFC")),
    ("AUTO", ("AUTO", "AUTOS"), ("AUTOMOB", "AUTOMOT")),
    ("PHARMA", (), ("PHARMA", "DRUG", "HEALTH", "BIOTECH")),
    ("FMCG", ("FMCG", "DEFENSIVE", "STAPLES"), ("FOOD", "BEVERAGE", "TOBACCO", "HOUSEHOLD", "PACKAGED")),
    ("METAL", ("METAL", "METALS", "STEEL", "MINING", "ALUMINUM", "ALUMINIUM"), ()),
    ("POWER", ("POWER", "UTILITIES", "UTILITY"), ()),
    ("ENERGY", ("ENERGY", "OIL", "GAS"), ()),
    ("RETAIL", (), ("RETAIL",)),
)
# "Consumer Cyclical / Discretionary / Durables" is not FMCG; a bare "Consumer" (older payloads) still is.
_CONSUMER_NOT_FMCG = ("CYCLICAL", "DISCRETIONARY", "DURABLE", "DURABLES")


def _sector_from_text(text: Any) -> Optional[str]:
    """Sector key for one sector/industry string, or None when no sector word is in it."""
    if not isinstance(text, str):
        return None            # None / numbers / lists are "no sector data", not text to stringify and match
    words = re.findall(r"[A-Z0-9]+", text.upper())
    if not words:
        return None
    for key, exact, prefixes in _SECTOR_WORDS:
        for w in words:
            if w in exact or any(w.startswith(pre) for pre in prefixes):
                return key
    if "CONSUMER" in words and not any(w in _CONSUMER_NOT_FMCG for w in words):
        return "FMCG"
    return None


def detect_sector(fundamentals: Dict[str, Any]) -> str:
    """Best-effort sector key from a fundamentals payload.

    2026-10-04 (log-audit item 10): looks at the more specific `industry` first, then `sector`, then
    `sectorDisp`, and uses the first one that names a known sector, so Yahoo's "Consumer Cyclical" +
    "Auto Manufacturers" is AUTO (it used to be FMCG) and "Utilities" is POWER (it used to be IT).
    Anything unrecognised is still "DEFAULT"."""
    for field in ("industry", "sector", "sectorDisp"):
        found = _sector_from_text(fundamentals.get(field))
        if found:
            return found
    return "DEFAULT"


def has_sector_data(fundamentals: Any) -> bool:
    """True when the payload carries at least one non-blank sector / industry / sectorDisp string.

    group101 (log-audit item 10): with none, detect_sector() still answers "DEFAULT", and the DEFAULT list
    (RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK) would be compared against a company whose sector is unknown,
    which says nothing about it. Callers use this to compare against no peers instead (unless the caller
    passed an explicit peer list). An unrecognised but present sector (e.g. "Agriculture") is not affected."""
    if not isinstance(fundamentals, dict):
        return False
    return any(isinstance(fundamentals.get(k), str) and fundamentals.get(k).strip()
               for k in ("industry", "sector", "sectorDisp"))


def peers_for(symbol: str, stock_fund: Any, sector: str, peers: Optional[List[str]], max_peers: int) -> List[str]:
    """build_peer_list(), except: no caller-supplied peers and no sector data at all -> no peers."""
    if not peers and not has_sector_data(stock_fund):
        return []
    return build_peer_list(symbol, sector, peers, max_peers)


def _fund_cache_get(symbol: str) -> Optional[Dict[str, Any]]:
    # 2026-10-04 (log-audit item 4): key by the canonical ".NS"/".BO" form so "INFY"
    # and "INFY.NS" share ONE entry (they used to be two keys -> two market-data calls).
    symbol = _norm_symbol(symbol)
    with _FUND_CACHE_LOCK:
        entry = _FUND_CACHE.get(symbol)
    if not entry:
        return None
    fetched_at, data = entry
    if (time.time() - fetched_at) > _FUND_CACHE_TTL:
        return None
    return data


def _fund_cache_set(symbol: str, data: Dict[str, Any]) -> None:
    symbol = _norm_symbol(symbol)
    with _FUND_CACHE_LOCK:
        _FUND_CACHE[symbol] = (time.time(), data)


# One lock per canonical symbol so two threads asking for the same symbol at the
# same moment make ONE market-data call (the second waits, then reads the cache).
_FUND_INFLIGHT: Dict[str, threading.Lock] = {}


def _fund_inflight_lock(symbol: str) -> threading.Lock:
    with _FUND_CACHE_LOCK:
        lock = _FUND_INFLIGHT.get(symbol)
        if lock is None:
            lock = _FUND_INFLIGHT[symbol] = threading.Lock()
        return lock


def fetch_fundamentals(market_data_url: str, symbol: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Fetch fundamentals from market-data-service (short-TTL cached — see
    module docstring above for why)."""
    # Always ask market-data for the canonical form, whichever spelling the caller used.
    symbol = _norm_symbol(symbol)
    cached = _fund_cache_get(symbol)
    if cached is not None:
        return cached
    with _fund_inflight_lock(symbol):
        cached = _fund_cache_get(symbol)      # another thread may have just fetched it
        if cached is not None:
            return cached
        try:
            url = f"{market_data_url.rstrip('/')}/fundamentals/{symbol}"
            resp = httpx.get(url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json() or {}
                _fund_cache_set(symbol, data)
                return data
        except Exception as e:
            logger.warning("Fundamentals fetch failed for %s: %s", symbol, e)
        return {}


def fetch_fundamentals_batch(
    market_data_url: str, symbols: List[str], timeout: float = 15.0
) -> Dict[str, Dict[str, Any]]:
    """Fetch fundamentals for several symbols concurrently (cache-aware).
    Returns {symbol: data}; a symbol whose fetch failed maps to {}.
    """
    result: Dict[str, Dict[str, Any]] = {}
    to_fetch = []
    seen_canon = set()
    dup_of: Dict[str, str] = {}     # spelling -> first spelling with the same canonical symbol
    first_for: Dict[str, str] = {}
    for s in symbols:
        cached = _fund_cache_get(s)
        if cached is not None:
            result[s] = cached
            continue
        canon = _norm_symbol(s)
        if canon in seen_canon:
            dup_of[s] = first_for[canon]       # "INFY" + "INFY.NS" -> one fetch
            continue
        seen_canon.add(canon)
        first_for[canon] = s
        to_fetch.append(s)

    if not to_fetch:
        for s, first in dup_of.items():
            result[s] = result.get(first, {})
        return result

    with ThreadPoolExecutor(max_workers=min(_FUND_FETCH_MAX_WORKERS, len(to_fetch))) as pool:
        futures = {pool.submit(fetch_fundamentals, market_data_url, s, timeout): s for s in to_fetch}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                result[s] = fut.result()
            except Exception as e:
                logger.warning("Batch fundamentals fetch failed for %s: %s", s, e)
                result[s] = {}
    for s, first in dup_of.items():
        result[s] = result.get(first, {})
    return result


def compute_peer_relative(
    symbol: str,
    stock_fund: Dict[str, Any],
    market_data_url: str,
    peers: Optional[List[str]] = None,
    max_peers: int = DEFAULT_MAX_PEERS,
) -> Dict[str, Any]:
    """
    Compare stock vs sector peers on PE, ROE, growth.

    Returns relative metrics and a simple peer_score (0–100).
    Higher = more attractive vs peers (cheaper PE, higher ROE/growth).
    """
    symbol = _norm_symbol(symbol)
    sector = detect_sector(stock_fund)
    peer_list = peers_for(symbol, stock_fund, sector, peers, max_peers)

    stock_pe = _pick(stock_fund, _PE_KEYS, skip_zero=True)
    stock_roe = _pick(stock_fund, _ROE_KEYS)
    stock_rev_g = _pick(stock_fund, _REV_G_KEYS)
    stock_profit_g = _pick(stock_fund, _PROFIT_G_KEYS)

    peer_pes, peer_roes, peer_rev, peer_profit = [], [], [], []
    peer_details = []

    # Fetch all peers concurrently (cache-aware) instead of one at a time —
    # see module docstring above.
    fetched = fetch_fundamentals_batch(market_data_url, peer_list)
    for p in peer_list:
        f = fetched.get(p) or {}
        if not f:
            continue
        pe = _pick(f, _PE_KEYS, skip_zero=True)
        roe = _pick(f, _ROE_KEYS)
        rg = _pick(f, _REV_G_KEYS)
        pg = _pick(f, _PROFIT_G_KEYS)
        if pe > 0:
            peer_pes.append(pe)
        if roe != 0:
            peer_roes.append(roe)
        if rg != 0:
            peer_rev.append(rg)
        if pg != 0:
            peer_profit.append(pg)
        peer_details.append({"symbol": p, "pe": pe, "roe": roe, "rev_g": rg, "profit_g": pg})

    def _avg(xs: List[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    avg_pe = _avg(peer_pes)
    avg_roe = _avg(peer_roes)
    avg_rev = _avg(peer_rev)
    avg_profit = _avg(peer_profit)

    # Relative ratios (stock / peer). PE lower is better → invert for score.
    pe_rel = (avg_pe / stock_pe) if stock_pe > 0 and avg_pe > 0 else 1.0
    roe_rel = (stock_roe / avg_roe) if avg_roe != 0 else 1.0
    rev_rel = (stock_rev_g / avg_rev) if avg_rev != 0 else 1.0
    profit_rel = (stock_profit_g / avg_profit) if avg_profit != 0 else 1.0

    # Simple 0–100 peer score
    # PE component: pe_rel > 1 means cheaper than peers
    pe_score = max(0.0, min(100.0, 50.0 + (pe_rel - 1.0) * 40.0))
    roe_score = max(0.0, min(100.0, 50.0 + (roe_rel - 1.0) * 40.0))
    growth_score = max(0.0, min(100.0, 50.0 + ((rev_rel + profit_rel) / 2.0 - 1.0) * 40.0))
    peer_score = round(0.35 * pe_score + 0.35 * roe_score + 0.30 * growth_score, 2)

    return {
        "sector": sector,
        "peers_used": [p["symbol"] for p in peer_details],
        "stock": {
            "pe": stock_pe,
            "roe": stock_roe,
            "revenue_growth_yoy": stock_rev_g,
            "profit_growth_yoy": stock_profit_g,
        },
        "peer_avg": {
            "pe": round(avg_pe, 2),
            "roe": round(avg_roe, 2),
            "revenue_growth_yoy": round(avg_rev, 2),
            "profit_growth_yoy": round(avg_profit, 2),
        },
        "relative": {
            "pe_rel": round(pe_rel, 3),       # >1 = cheaper than peers
            "roe_rel": round(roe_rel, 3),     # >1 = higher ROE
            "rev_growth_rel": round(rev_rel, 3),
            "profit_growth_rel": round(profit_rel, 3),
        },
        "peer_score": peer_score,  # 0–100
        "peer_details": peer_details,
    }


def compute_multi_quarter_consistency(
    quarterly: Optional[List[Dict[str, Any]]] = None,
    fundamentals: Optional[Dict[str, Any]] = None,
    min_quarters: int = 2,
) -> Dict[str, Any]:
    """
    Check consistency of revenue / profit growth across last 2–3 quarters.

    `quarterly` expected shape (flexible):
      [{"period": "2025-Q1", "revenue_growth": 12.5, "profit_growth": 8.1}, ...]
      newest first or oldest first — both handled.

    If quarterly list is missing, falls back to YoY fields on fundamentals
    and returns a weaker consistency signal.
    """
    fundamentals = fundamentals or {}
    result = {
        "quarters_checked": 0,
        "positive_revenue_quarters": 0,
        "positive_profit_quarters": 0,
        "consistent_revenue": False,
        "consistent_profit": False,
        "consistent_both": False,
        "consistency_score": 50.0,  # neutral default
        "avg_revenue_growth": 0.0,
        "avg_profit_growth": 0.0,
        "detail": [],
    }

    rows: List[Dict[str, Any]] = []
    if quarterly:
        for q in quarterly:
            rg = _pick(q, ("revenue_growth", "revenue_growth_yoy", "revenueGrowth"))
            pg = _pick(q, ("profit_growth", "profit_growth_yoy", "earningsGrowth", "net_income_growth"))
            rows.append({
                "period": q.get("period") or q.get("date") or q.get("quarter"),
                "revenue_growth": rg,
                "profit_growth": pg,
            })

    # Fallback: single YoY numbers → treat as 1 "quarter" signal
    if not rows:
        rg = _pick(fundamentals, _REV_G_KEYS)
        pg = _pick(fundamentals, _PROFIT_G_KEYS)
        if rg != 0 or pg != 0:
            rows = [{"period": "TTM/YoY", "revenue_growth": rg, "profit_growth": pg}]

    if not rows:
        return result

    # Use last min_quarters (prefer most recent)
    rows = rows[: max(min_quarters, 3)]
    result["quarters_checked"] = len(rows)
    result["detail"] = rows

    pos_rev = sum(1 for r in rows if r["revenue_growth"] > 0)
    pos_profit = sum(1 for r in rows if r["profit_growth"] > 0)
    result["positive_revenue_quarters"] = pos_rev
    result["positive_profit_quarters"] = pos_profit
    result["avg_revenue_growth"] = round(sum(r["revenue_growth"] for r in rows) / len(rows), 2)
    result["avg_profit_growth"] = round(sum(r["profit_growth"] for r in rows) / len(rows), 2)

    need = min(min_quarters, len(rows))
    result["consistent_revenue"] = pos_rev >= need
    result["consistent_profit"] = pos_profit >= need
    result["consistent_both"] = result["consistent_revenue"] and result["consistent_profit"]

    # Score 0–100
    # Base from fraction of positive quarters + bonus for both consistent
    frac = (pos_rev + pos_profit) / (2.0 * len(rows)) if rows else 0.5
    score = 40.0 + frac * 50.0
    if result["consistent_both"]:
        score += 10.0
    if result["avg_revenue_growth"] > 15 and result["avg_profit_growth"] > 10:
        score += 5.0
    result["consistency_score"] = round(max(0.0, min(100.0, score)), 2)

    return result


def enrich_fundamentals_with_peer_and_consistency(
    symbol: str,
    fundamentals: Dict[str, Any],
    market_data_url: str,
    quarterly: Optional[List[Dict[str, Any]]] = None,
    peers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    One-shot helper: attach peer_relative + multi_quarter to a fundamentals dict.
    Safe to call from fundamental-analysis or decision engine.
    """
    out = dict(fundamentals or {})
    try:
        out["peer_relative"] = compute_peer_relative(
            symbol, out, market_data_url, peers=peers
        )
    except Exception as e:
        logger.warning("Peer relative failed for %s: %s", symbol, e)
        out["peer_relative"] = {"peer_score": 50.0, "error": str(e)}

    try:
        out["multi_quarter"] = compute_multi_quarter_consistency(
            quarterly=quarterly, fundamentals=out, min_quarters=2
        )
    except Exception as e:
        logger.warning("Multi-quarter failed for %s: %s", symbol, e)
        out["multi_quarter"] = {"consistency_score": 50.0, "error": str(e)}

    # Convenience flat fields for prediction / scoring
    out["peer_score"] = _safe(out.get("peer_relative", {}).get("peer_score"), 50.0)
    out["consistency_score"] = _safe(out.get("multi_quarter", {}).get("consistency_score"), 50.0)
    out["consistent_growth"] = bool(out.get("multi_quarter", {}).get("consistent_both"))

    return out
