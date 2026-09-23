"""
Wire peer ranking + multi-quarter enrichment into fundamental analyze response.

Drop-in for services/analysis-intelligence-service/fundamental/main.py

Usage at end of analyze():
    from wire_peer_multi_quarter import apply_to_analyze_response
    result = apply_to_analyze_response(symbol=symbol, analyze_payload=result, market_data_url=MARKET_DATA_URL)
    return result
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("wire_peer_mq")

try:
    from peer_multi_quarter import enrich_fundamentals_with_peer_and_consistency
except ImportError:
    enrich_fundamentals_with_peer_and_consistency = None

try:
    from peer_ranking import rank_against_peers
except ImportError:
    rank_against_peers = None

try:
    from peers import peers_for, normalize_sector, peer_relative_score, average_metrics
except ImportError:
    peers_for = normalize_sector = peer_relative_score = average_metrics = None

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "").rstrip("/")


def apply_to_analyze_response(
    symbol: str,
    analyze_payload: Dict[str, Any],
    market_data_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Enrich an existing fundamental analyze() result with:
      - stronger peer ranking table
      - consistency fields aligned for prediction features
      - optional score blend

    Safe: never raises; on failure returns original payload.
    """
    try:
        payload = dict(analyze_payload or {})
        md_url = (market_data_url or MARKET_DATA_URL or "").rstrip("/")
        metrics = payload.get("metrics") or {}
        raw = payload.get("raw") or {}
        fund = {**raw, **metrics}

        multi_q = payload.get("multi_quarter_detail") or {}
        payload["consistency_score"] = float(
            payload.get("multi_quarter_score")
            or multi_q.get("score")
            or 50.0
        )
        payload["consistent_growth"] = bool(
            payload.get("multi_quarter_ok") or multi_q.get("ok")
        )

        peer_score = payload.get("peer_relative_score")
        if peer_score is None and isinstance(payload.get("peer_relative"), dict):
            peer_score = payload["peer_relative"].get("score")
        peer_score = float(peer_score) if peer_score is not None else 50.0

        ranking = None
        if rank_against_peers is not None and md_url:
            try:
                ranking = rank_against_peers(
                    symbol=symbol,
                    stock_fund=fund,
                    market_data_url=md_url,
                    peers=payload.get("peer_list"),
                )
                if ranking.get("peer_score") is not None:
                    peer_score = float(ranking["peer_score"])
            except Exception as e:
                logger.warning("rank_against_peers failed: %s", e)

        if enrich_fundamentals_with_peer_and_consistency is not None and md_url:
            try:
                enriched = enrich_fundamentals_with_peer_and_consistency(
                    symbol=symbol,
                    fundamentals=fund,
                    market_data_url=md_url,
                    quarterly=None,
                    peers=payload.get("peer_list"),
                )
                if enriched.get("peer_score") is not None:
                    peer_score = float(enriched["peer_score"])
                if enriched.get("consistency_score") is not None:
                    payload["consistency_score"] = float(enriched["consistency_score"])
                if "consistent_growth" in enriched:
                    payload["consistent_growth"] = bool(enriched["consistent_growth"])
                if enriched.get("peer_relative"):
                    payload["peer_relative"] = enriched["peer_relative"]
                if enriched.get("multi_quarter"):
                    payload["multi_quarter"] = enriched["multi_quarter"]
            except Exception as e:
                logger.warning("enrich_fundamentals failed: %s", e)

        payload["peer_score"] = round(peer_score, 2)

        if ranking:
            payload["peer_ranking"] = ranking
            payload["peer_rank"] = ranking.get("rank")
            payload["peer_rank_label"] = ranking.get("rank_label")
            payload["peer_count"] = ranking.get("total_compared")

        base = payload.get("fundamental_score")
        if isinstance(base, (int, float)):
            payload["fundamental_score_raw"] = float(base)
            cons = float(payload.get("consistency_score") or 50.0)
            adjusted = 0.70 * float(base) + 0.20 * peer_score + 0.10 * cons
            payload["fundamental_score"] = round(max(0.0, min(100.0, adjusted)), 2)
            payload["fundamental_score_adjusted"] = True

        return payload
    except Exception as e:
        logger.exception("apply_to_analyze_response failed: %s", e)
        return analyze_payload
