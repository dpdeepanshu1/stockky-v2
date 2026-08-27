"""
candidate_engine/candidates.py — pulls recommendations from the EXISTING
api-gateway. Read-only, HTTP-only: this module never talks to Dhan, never
writes to the existing Stockky services' data, and never places an order
itself — it only turns "what api-gateway is already recommending" into
trade_candidates rows for entry_engine (Phase 2) to evaluate.

Per the architecture note in the plan: the existing decision engine's job
stays "find and analyse opportunities"; this service's job is "decide
whether/how/when to trade and safely execute". This file is the seam
between the two.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

import config
import models

logger = logging.getLogger("real-trade-candidates")

# Which existing endpoints feed candidates, and how to normalize each
# response shape into (symbol, decision_label, conviction_score, price).
# Kept data-driven so adding a 4th source tab later is a dict entry, not a
# new code path.
_SOURCES = {
    "hot_picks": "/stockky-hot",
    "ipo": "/surprise/ipo/list",
    # NOTE: Surprise Momentum tab is deliberately NOT wired in yet.
    # /surprise/static returns baseline stats (price/volume history used
    # to DETECT a surprise), not the scored decision list the tab
    # displays — that only exists via /surprise/scan, which triggers a
    # full live scan cycle on every call. Polling that from here on a
    # refresh cadence would duplicate/compete with the tab's own scans
    # for no benefit. Once api-gateway has a cheap "last scored result,
    # cached" read endpoint for Surprise (the way /stockky-hot already
    # is for Hot Picks), add it here the same way. Wiring a guessed shape
    # against the wrong endpoint would be worse than leaving this out —
    # it would look connected while silently reading meaningless rows.
}


async def _fetch(client: httpx.AsyncClient, path: str) -> Any:
    url = f"{config.API_GATEWAY_URL}{path}"
    try:
        r = await client.get(url, timeout=25.0)  # api-gateway can block briefly on a
        # synchronous NSE scrape (see its own market-movers logs) — 15s was
        # tight enough to time out on nothing more than bad luck; 25s gives
        # it room without hanging a whole REAL cycle indefinitely.
        if r.status_code == 200:
            return r.json()
        logger.warning("candidate fetch %s -> HTTP %s: %s", url, r.status_code, r.text[:200])
    except Exception as e:
        # httpx's own timeout/connect exceptions frequently stringify to ""
        # (a known httpx quirk) — always include the exception TYPE too, or
        # a bare "failed: " tells you nothing about whether this was a DNS
        # failure, a connection refusal, or api-gateway just being slow.
        logger.warning("candidate fetch %s failed: %s: %s", url, type(e).__name__, e)
    return None


def _rows_from_hot_picks(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    out = []
    for bucket in ("bulk_insider_driven", "results_driven", "news_driven"):
        for item in payload.get(bucket) or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            out.append({
                "symbol": item["symbol"],
                "source_tab": "hot_picks",
                "decision_label": item.get("decision"),
                "conviction_score": item.get("score"),
                "signal_price": item.get("price") or item.get("close"),
                "raw_payload": item,
            })
    return out


def _rows_from_ipo(payload: Any) -> list[dict]:
    items = payload if isinstance(payload, list) else (payload or {}).get("items", [])
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        out.append({
            "symbol": item["symbol"],
            "source_tab": "ipo",
            "decision_label": item.get("decision"),
            "conviction_score": item.get("score"),
            "signal_price": item.get("cmp") or item.get("price"),
            "raw_payload": item,
        })
    return out


async def refresh_candidates(db: Session, mode: str) -> int:
    """Fetch every source, normalize, and insert new trade_candidates rows.
    Does NOT deduplicate against already-`consumed` rows for the same
    symbol here — entry_engine (Phase 2) is where "do we already have an
    open position / pending order for this symbol" gets decided, since
    that requires portfolio state this module deliberately doesn't hold.
    Returns the number of rows inserted."""
    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        hot = await _fetch(client, _SOURCES["hot_picks"])
        rows += _rows_from_hot_picks(hot or {})

        ipo = await _fetch(client, _SOURCES["ipo"])
        rows += _rows_from_ipo(ipo)

    inserted = 0
    for r in rows:
        db.add(models.TradeCandidate(
            mode=mode,
            symbol=r["symbol"],
            source_tab=r["source_tab"],
            decision_label=r.get("decision_label"),
            conviction_score=r.get("conviction_score"),
            signal_price=r.get("signal_price"),
            raw_payload=json.dumps(r.get("raw_payload") or {}),
        ))
        inserted += 1
    if inserted:
        db.commit()
    logger.info("candidate_engine: refreshed %d candidate rows for mode=%s", inserted, mode)
    return inserted
