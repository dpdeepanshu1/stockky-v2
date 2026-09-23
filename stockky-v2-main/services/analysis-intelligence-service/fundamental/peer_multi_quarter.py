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
_FUND_CACHE_TTL = float(os.getenv("PEER_FUNDAMENTALS_CACHE_TTL_SECONDS", "60"))
_FUND_FETCH_MAX_WORKERS = int(os.getenv("PEER_FUNDAMENTALS_FETCH_WORKERS", "6"))

# Default peer maps for common Indian sectors (extend as needed)
DEFAULT_PEERS: Dict[str, List[str]] = {
    "IT": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
    "BANK": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS"],
    "AUTO": ["MARUTI.NS", "TATAMOTORS.NS", "M&M.NS", "BAJAJ-AUTO.NS", "HEROMOTOCO.NS"],
    "PHARMA": ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "APOLLOHOSP.NS"],
    "FMCG": ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS"],
    "METAL": ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS", "COALINDIA.NS"],
    "ENERGY": ["RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "GAIL.NS"],
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


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s.endswith(".NS") and not s.endswith(".BO"):
        s = f"{s}.NS"
    return s


def detect_sector(fundamentals: Dict[str, Any]) -> str:
    """Best-effort sector from fundamental payload."""
    sector = (
        fundamentals.get("sector")
        or fundamentals.get("industry")
        or fundamentals.get("sectorDisp")
        or ""
    ).upper()
    if any(k in sector for k in ("IT", "SOFTWARE", "TECH")):
        return "IT"
    if any(k in sector for k in ("BANK", "FINANCIAL", "NBFC")):
        return "BANK"
    if any(k in sector for k in ("AUTO", "AUTOMOBILE")):
        return "AUTO"
    if any(k in sector for k in ("PHARMA", "DRUG", "HEALTH")):
        return "PHARMA"
    if any(k in sector for k in ("FMCG", "CONSUMER")):
        return "FMCG"
    if any(k in sector for k in ("METAL", "STEEL", "MINING")):
        return "METAL"
    if any(k in sector for k in ("ENERGY", "OIL", "GAS", "POWER")):
        return "ENERGY"
    return "DEFAULT"


def _fund_cache_get(symbol: str) -> Optional[Dict[str, Any]]:
    with _FUND_CACHE_LOCK:
        entry = _FUND_CACHE.get(symbol)
    if not entry:
        return None
    fetched_at, data = entry
    if (time.time() - fetched_at) > _FUND_CACHE_TTL:
        return None
    return data


def _fund_cache_set(symbol: str, data: Dict[str, Any]) -> None:
    with _FUND_CACHE_LOCK:
        _FUND_CACHE[symbol] = (time.time(), data)


def fetch_fundamentals(market_data_url: str, symbol: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Fetch fundamentals from market-data-service (short-TTL cached — see
    module docstring above for why)."""
    cached = _fund_cache_get(symbol)
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
    for s in symbols:
        cached = _fund_cache_get(s)
        if cached is not None:
            result[s] = cached
        else:
            to_fetch.append(s)

    if not to_fetch:
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
    return result


def compute_peer_relative(
    symbol: str,
    stock_fund: Dict[str, Any],
    market_data_url: str,
    peers: Optional[List[str]] = None,
    max_peers: int = 5,
) -> Dict[str, Any]:
    """
    Compare stock vs sector peers on PE, ROE, growth.

    Returns relative metrics and a simple peer_score (0–100).
    Higher = more attractive vs peers (cheaper PE, higher ROE/growth).
    """
    symbol = _norm_symbol(symbol)
    sector = detect_sector(stock_fund)
    peer_list = peers or DEFAULT_PEERS.get(sector, DEFAULT_PEERS["DEFAULT"])
    peer_list = [p for p in peer_list if _norm_symbol(p) != symbol][:max_peers]

    stock_pe = _safe(stock_fund.get("pe_ratio") or stock_fund.get("trailingPE") or stock_fund.get("pe"))
    stock_roe = _safe(stock_fund.get("roe") or stock_fund.get("returnOnEquity"))
    stock_rev_g = _safe(stock_fund.get("revenue_growth_yoy") or stock_fund.get("revenueGrowth"))
    stock_profit_g = _safe(stock_fund.get("profit_growth_yoy") or stock_fund.get("earningsGrowth"))

    peer_pes, peer_roes, peer_rev, peer_profit = [], [], [], []
    peer_details = []

    # Fetch all peers concurrently (cache-aware) instead of one at a time —
    # see module docstring above.
    fetched = fetch_fundamentals_batch(market_data_url, peer_list)
    for p in peer_list:
        f = fetched.get(p) or {}
        if not f:
            continue
        pe = _safe(f.get("pe_ratio") or f.get("trailingPE") or f.get("pe"))
        roe = _safe(f.get("roe") or f.get("returnOnEquity"))
        rg = _safe(f.get("revenue_growth_yoy") or f.get("revenueGrowth"))
        pg = _safe(f.get("profit_growth_yoy") or f.get("earningsGrowth"))
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
            rg = q.get("revenue_growth") or q.get("revenue_growth_yoy") or q.get("revenueGrowth")
            pg = q.get("profit_growth") or q.get("profit_growth_yoy") or q.get("earningsGrowth") or q.get("net_income_growth")
            rows.append({
                "period": q.get("period") or q.get("date") or q.get("quarter"),
                "revenue_growth": _safe(rg),
                "profit_growth": _safe(pg),
            })

    # Fallback: single YoY numbers → treat as 1 "quarter" signal
    if not rows:
        rg = _safe(fundamentals.get("revenue_growth_yoy") or fundamentals.get("revenueGrowth"))
        pg = _safe(fundamentals.get("profit_growth_yoy") or fundamentals.get("earningsGrowth"))
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
