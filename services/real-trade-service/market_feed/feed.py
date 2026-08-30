"""
market_feed/feed.py — price source for entry/exit evaluation.

DESIGN DECISION (Phase 2): DEMO mode reads prices from Stockky's EXISTING
market-data-service — the same free, already-deployed yfinance-backed
service every other tab uses — rather than Dhan's feed. Reasoning:

  * Dhan's market/quote endpoints require a connected account + valid
    access token, same as the trading API. DEMO mode is meant to work
    for anyone trying the system out BEFORE they've linked a real Dhan
    account — gating paper trading behind a real brokerage login would
    defeat the point of a risk-free rehearsal mode.
  * market-data-service already solves symbol resolution, delisted-symbol
    handling, and multi-source fallback (see the whole symbol_aliases.py
    robustness work from this session) — reusing it means DEMO fills are
    priced off real, already-hardened infrastructure instead of a second,
    parallel price pipeline that would need the same hardening again.

REAL mode's execution path (Phase 3) will read prices from Dhan's own feed
via execution/dhan_client.py once wired — the two paths are intentionally
decoupled: this module is DEMO-only. A REAL order's actual fill price
always comes from Dhan itself, never from this module.

This is a POLLING feed (HTTP GET on an interval), not a persistent
WebSocket — simpler, and the existing market-data-service doesn't expose a
streaming endpoint. The plan's "real-time chart via Dhan" is unaffected:
that's a REAL-mode-connected feature for Phase 3, once Dhan is linked.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

logger = logging.getLogger("real-trade-market-feed")

# Mirrors every other Stockky service's MARKET_DATA_URL env convention
# (decision-prediction-service/decision/main.py sets the same default host)
# rather than guessing/deriving one from API_GATEWAY_URL.
# §1 — max age for live_quotes rows before we consider them stale.
# real-trade-service staleness guard: if the live_quotes row is older than
# this, fall through to the yfinance-backed market-data-service /quote endpoint.
LIVE_QUOTE_MAX_AGE_S = float(os.getenv("LIVE_QUOTE_MAX_AGE_S", "5.0"))

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")


class Tick:
    __slots__ = ("symbol", "price", "as_of", "atr", "source")

    def __init__(self, symbol: str, price: float, as_of: datetime, atr: Optional[float], source: str):
        self.symbol = symbol
        self.price = price
        self.as_of = as_of
        self.atr = atr
        self.source = source


async def get_quote(client: httpx.AsyncClient, symbol: str) -> Optional[Tick]:
    """
    §1 — Quote with live_quotes-first cascade:
      1. live_quotes table (AngelOne WS feed, freshness ≤ LIVE_QUOTE_MAX_AGE_S)
      2. market-data-service /quote/{symbol} (yfinance-backed, existing)

    The staleness guard: if the live_quotes row is older than LIVE_QUOTE_MAX_AGE_S
    seconds, treat it as stale and fall through to source 2. This blocks trading
    on frozen data rather than acting on a stale AngelOne tick.
    """
    # ── Source 1: live_quotes table ────────────────────────────────────────────
    try:
        r_lq = await client.get(f"{MARKET_DATA_URL}/live-quote/{symbol}", timeout=3.0)
        if r_lq.status_code == 200:
            lq = r_lq.json()
            ltp = lq.get("ltp")
            updated_at_str = lq.get("updated_at")
            if ltp and float(ltp) > 0 and updated_at_str:
                from dateutil import parser as _dp
                updated_at = _dp.parse(updated_at_str)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age_s <= LIVE_QUOTE_MAX_AGE_S:
                    ohlc = lq.get("ohlc") or {}
                    return Tick(
                        symbol=symbol,
                        price=float(ltp),
                        as_of=updated_at,
                        atr=None,  # ATR comes from history, not tick feed
                        source=f"live_quotes({lq.get('source','angelone')})",
                    )
                else:
                    logger.debug(
                        "live_quotes %s is %.1fs old > %.1fs limit — falling through",
                        symbol, age_s, LIVE_QUOTE_MAX_AGE_S,
                    )
    except Exception as e:
        logger.debug("live_quotes read failed for %s (non-fatal): %s", symbol, e)

    # ── Source 2: market-data-service /quote (yfinance-backed) ─────────────────
    try:
        r = await client.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=8.0)
        if r.status_code != 200:
            return None
        q = r.json()
        price = q.get("price") or q.get("cmp")
        if not price or float(price) <= 0:
            return None
        return Tick(
            symbol=symbol,
            price=float(price),
            # market-data-service's own fetched_at isn't guaranteed to be a
            # cleanly-parseable tz-aware timestamp across every source
            # branch it can take, so this module stamps its OWN receipt
            # time — which is what risk_engine's staleness check (#7) is
            # actually trying to measure (age since WE last saw a price),
            # not the upstream provider's internal timestamp.
            as_of=datetime.now(timezone.utc),
            atr=float(q["atr"]) if q.get("atr") else None,
            source=q.get("source") or "market-data-service",
        )
    except Exception as e:
        logger.debug("get_quote(%s) source-2 failed: %s", symbol, e)
        return None


async def get_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Bulk fetch — concurrent requests over one shared client, not
    market-data-service's /quotes/bulk (that endpoint is yf.download-shaped
    with a different response contract; for the handful of symbols one
    entry/exit cycle touches, N concurrent /quote calls on a shared
    keep-alive connection is simple and fast enough for Phase 2)."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[get_quote(client, s) for s in symbols])
    for sym, tick in zip(symbols, results):
        if tick is not None:
            out[sym] = tick
    return out
