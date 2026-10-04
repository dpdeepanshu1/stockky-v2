"""
Peer ranking metrics (free-tier).

Ranks a stock against its sector peers on PE, ROE, growth and a combined score.
Designed to be called from fundamental analysis and exposed in API responses.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from peer_multi_quarter import (
    DEFAULT_MAX_PEERS,
    _norm_symbol,
    _PE_KEYS,
    _PROFIT_G_KEYS,
    _REV_G_KEYS,
    _ROE_KEYS,
    _pick,
    build_peer_list,
    compute_peer_relative,
    detect_sector,
    has_sector_data,
    peers_for,
    fetch_fundamentals,
    fetch_fundamentals_batch,
)

logger = logging.getLogger("peer_ranking")


def rank_against_peers(
    symbol: str,
    stock_fund: Dict[str, Any],
    market_data_url: str,
    peers: Optional[List[str]] = None,
    max_peers: int = DEFAULT_MAX_PEERS,
) -> Dict[str, Any]:
    """
    Build a peer ranking table and rank position for the stock.

    Returns:
      - peer_score (0-100)
      - rank (1 = best among peers+self on combined metric)
      - total_compared (the symbol plus every peer that returned data)
      - peers_compared: number of peers actually ranked against the stock (0 = no comparison;
        rank_label then reads "No peers compared in <sector>" instead of "#1 of 1")
      - peers_skipped: peers whose fundamentals fetch returned nothing (not ranked)
      - ranking_table: list of {symbol, pe, roe, rev_g, profit_g, combined, is_self}
      - metrics used for ranking explanation
    """
    symbol = _norm_symbol(symbol)
    sector = detect_sector(stock_fund)
    if not peers and not has_sector_data(stock_fund):
        sector = "UNKNOWN"      # no sector/industry in the payload: reported as unknown, no generic peers
    # build_peer_list (shared with compute_peer_relative) normalises, drops
    # duplicates (e.g. "TCS" and "TCS.NS" in the same list) and takes the symbol
    # out of the peer list; the symbol is then always put first so it can never
    # be sliced off by max_peers (it used to vanish when the caller's list had it
    # past position max_peers, leaving no is_self row and reporting the top PEER
    # as "self"). Zero or negative max_peers means no peers.
    peer_list = [symbol] + peers_for(symbol, stock_fund, sector, peers, max_peers)

    # Fix (30 Aug 2026): fetch all non-self peers concurrently (cache-aware,
    # shared with compute_peer_relative below) instead of one blocking
    # request at a time — see peer_multi_quarter.py module docstring for
    # why this mattered for /fundamental/analyze latency.
    non_self_peers = [p for p in peer_list if p != symbol]
    fetched = fetch_fundamentals_batch(market_data_url, non_self_peers)

    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for p in peer_list:
        if p == symbol:
            f = stock_fund
        else:
            f = fetched.get(p)
            if not f:
                # Fetch failed / no data (fetch_fundamentals returns {}). compute_peer_relative
                # skips such peers; ranking used to list them with a made-up neutral 43.0,
                # which placed an unknown company above a real but weak one and counted it in
                # total_compared. They are now left out and reported in `peers_skipped`.
                skipped.append(p)
                continue
        # _pick, not `a or b`: a real 0.0 ROE / growth in the primary key must
        # not fall through to the alias key. P/E 0 still counts as missing.
        pe = _pick(f, _PE_KEYS, skip_zero=True)
        roe = _pick(f, _ROE_KEYS)
        rg = _pick(f, _REV_G_KEYS)
        pg = _pick(f, _PROFIT_G_KEYS)

        # Combined attractiveness: lower PE better, higher ROE/growth better
        # Normalize roughly without full z-score for free-tier simplicity
        # P/E handling (was `100 / (1 + max(pe, 0.1) / 20)`, which scored both a
        # loss-making company (negative P/E) and a MISSING P/E (_safe -> 0.0) as
        # the best possible P/E, so a peer whose fetch failed outranked healthy
        # peers):
        #   pe > 0   -> lower is better (P/E 20 = neutral 50)
        #   pe < 0   -> loss-making: worst P/E score, 0
        #   pe == 0  -> missing/unknown: neutral 50
        if pe > 0:
            pe_component = 100.0 / (1.0 + pe / 20.0)
        elif pe < 0:
            pe_component = 0.0
        else:
            pe_component = 50.0
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

    # With no peer in the table (all fetches failed, or none requested) the stock
    # "ranks" #1 of 1 against nothing, which reads as a sector win. Say so instead.
    # rank / total_compared keep their numeric values (1 / 1) for existing readers.
    peers_compared = sum(1 for r in rows_sorted if not r["is_self"])
    if peers_compared:
        rank_label = f"#{rank} of {len(rows_sorted)} in {sector}"
    else:
        rank_label = f"No peers compared in {sector}"

    # Also get detailed peer_relative for scores
    try:
        peer_rel = compute_peer_relative(
            symbol, stock_fund, market_data_url, peers=peers, max_peers=max_peers
        )
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
        "peers_compared": peers_compared,
        "peers_skipped": skipped,
        "rank_label": rank_label,
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
