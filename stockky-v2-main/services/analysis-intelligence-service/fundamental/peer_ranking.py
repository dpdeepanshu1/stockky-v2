"""
Peer ranking metrics (free-tier).

Ranks a stock against its sector peers on PE, ROE, growth and a combined score.
Designed to be called from fundamental analysis and exposed in API responses.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from peer_multi_quarter import (
    DEFAULT_PEERS,
    _norm_symbol,
    _safe,
    compute_peer_relative,
    detect_sector,
    fetch_fundamentals,
    fetch_fundamentals_batch,
)

logger = logging.getLogger("peer_ranking")


def rank_against_peers(
    symbol: str,
    stock_fund: Dict[str, Any],
    market_data_url: str,
    peers: Optional[List[str]] = None,
    max_peers: int = 6,
) -> Dict[str, Any]:
    """
    Build a peer ranking table and rank position for the stock.

    Returns:
      - peer_score (0-100)
      - rank (1 = best among peers+self on combined metric)
      - total_compared
      - ranking_table: list of {symbol, pe, roe, rev_g, profit_g, combined, is_self}
      - metrics used for ranking explanation
    """
    symbol = _norm_symbol(symbol)
    sector = detect_sector(stock_fund)
    peer_list = peers or DEFAULT_PEERS.get(sector, DEFAULT_PEERS["DEFAULT"])
    peer_list = [_norm_symbol(p) for p in peer_list]
    if symbol not in peer_list:
        peer_list = [symbol] + peer_list
    peer_list = peer_list[: max_peers + 1]

    # Fix (30 Aug 2026): fetch all non-self peers concurrently (cache-aware,
    # shared with compute_peer_relative below) instead of one blocking
    # request at a time — see peer_multi_quarter.py module docstring for
    # why this mattered for /fundamental/analyze latency.
    non_self_peers = [p for p in peer_list if p != symbol]
    fetched = fetch_fundamentals_batch(market_data_url, non_self_peers)

    rows: List[Dict[str, Any]] = []
    for p in peer_list:
        if p == symbol:
            f = stock_fund
        else:
            f = fetched.get(p) or {}
        pe = _safe(f.get("pe_ratio") or f.get("trailingPE") or f.get("pe"))
        roe = _safe(f.get("roe") or f.get("returnOnEquity"))
        rg = _safe(f.get("revenue_growth_yoy") or f.get("revenueGrowth"))
        pg = _safe(f.get("profit_growth_yoy") or f.get("earningsGrowth"))

        # Combined attractiveness: lower PE better, higher ROE/growth better
        # Normalize roughly without full z-score for free-tier simplicity
        pe_component = 100.0 / (1.0 + max(pe, 0.1) / 20.0)  # lower PE → higher
        roe_component = max(0.0, min(100.0, roe * 3.0)) if roe else 30.0
        growth_component = max(0.0, min(100.0, 50.0 + (rg + pg) / 2.0))
        combined = round(0.35 * pe_component + 0.35 * roe_component + 0.30 * growth_component, 2)

        rows.append({
            "symbol": p,
            "pe": round(pe, 2),
            "roe": round(roe, 2),
            "rev_g": round(rg, 2),
            "profit_g": round(pg, 2),
            "combined": combined,
            "is_self": p == symbol,
        })

    # Rank: higher combined = better (rank 1 = best)
    rows_sorted = sorted(rows, key=lambda r: r["combined"], reverse=True)
    rank = next((i + 1 for i, r in enumerate(rows_sorted) if r["is_self"]), len(rows_sorted))
    self_row = next((r for r in rows_sorted if r["is_self"]), rows_sorted[0] if rows_sorted else {})

    # Also get detailed peer_relative for scores
    try:
        peer_rel = compute_peer_relative(symbol, stock_fund, market_data_url, peers=peers)
        peer_score = peer_rel.get("peer_score", self_row.get("combined", 50.0))
    except Exception as e:
        logger.warning("peer_relative in ranking failed: %s", e)
        peer_rel = {}
        peer_score = self_row.get("combined", 50.0)

    return {
        "symbol": symbol,
        "sector": sector,
        "peer_score": round(float(peer_score), 2),
        "rank": rank,
        "total_compared": len(rows_sorted),
        "rank_label": f"#{rank} of {len(rows_sorted)} in {sector}",
        "self": self_row,
        "ranking_table": rows_sorted,
        "peer_relative": peer_rel,
        "metrics": {
            "pe_weight": 0.35,
            "roe_weight": 0.35,
            "growth_weight": 0.30,
            "note": "Lower PE and higher ROE/growth rank better",
        },
    }
