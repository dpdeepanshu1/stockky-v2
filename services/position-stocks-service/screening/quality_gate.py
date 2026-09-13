"""
screening/quality_gate.py — fast, best-effort fundamental/technical/news
pre-check applied to the top few price/volume candidates before entry.

WHY THIS EXISTS: the 1m/5m/15m/60m screener (screening/engine.py) only looks
at price and tick activity — it has no idea if a stock is moving because of
a genuine positive catalyst (good results, a bulk deal, positive news) versus
a fundamentally weak/illiquid name having a random spike. This module adds
that check — but ONLY for the handful of symbols the price/volume screen
already ranked highest (config.QUALITY_GATE_TOP_N), never the whole scan
universe, so it stays fast.

DESIGN — mirrors real-trade-service's candidate_engine/candidates.py
quality-gate pattern exactly, reusing the SAME shared analysis-intelligence-
service endpoints (a shared, read-only backend — not real-trade-service's
own state, so calling it doesn't compromise this service's isolation from
real-trade-service specifically):
  - GET {FUNDAMENTAL_URL}/analyze/{symbol}  -> fundamental_score, market_cap
  - GET {TECHNICAL_URL}/analyze/{symbol}    -> technical_score
  - GET {EVENT_URL}/events/{symbol}/categorized -> has_positive_catalyst,
    recent_event_score, bulk_deals (news/bulk-deal/earnings-surprise signal)

FAIL-OPEN, ALWAYS: every call has a short timeout (config.QUALITY_GATE_
TIMEOUT_S — a couple seconds, not real-trade-service's 12-60s, because this
loop ticks every 10s and anything slower isn't "quick"). Any exception,
timeout, or non-200 response leaves that field as None. A None field is
"unknown", never treated as a reject — same leniency real-trade-service's
own quality gate uses ("missing fresh fundamentals is common and shouldn't
reject an otherwise fine breakout on its own"). Only a field that IS present
and clearly below its floor causes a skip-this-one-try-next-candidate
outcome. If analysis-intelligence-service is down entirely, every check
comes back all-None and every candidate simply passes through unfiltered —
the scalp loop is never blocked or slowed by this being unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger("position-stocks-quality-gate")


@dataclass
class QualitySignal:
    symbol: str
    fundamental_score: Optional[float] = None
    technical_score: Optional[float] = None
    market_cap_cr: Optional[float] = None
    has_positive_catalyst: Optional[bool] = None
    recent_event_score: Optional[float] = None
    bulk_deal_flag: bool = False

    def passes(self) -> tuple[bool, str]:
        """Lenient pass/fail: only reject on data that IS present and below
        floor. Missing data always passes through with a reason noting so."""
        if self.fundamental_score is not None and self.fundamental_score < config.MIN_FUNDAMENTAL_SCORE:
            return False, f"fundamental_score {self.fundamental_score:.0f} < floor {config.MIN_FUNDAMENTAL_SCORE:.0f}"
        if self.technical_score is not None and self.technical_score < config.MIN_TECHNICAL_SCORE:
            return False, f"technical_score {self.technical_score:.0f} < floor {config.MIN_TECHNICAL_SCORE:.0f}"
        if self.market_cap_cr is not None and self.market_cap_cr < config.MIN_MARKET_CAP_CR:
            return False, f"market_cap ₹{self.market_cap_cr:.0f}cr < floor ₹{config.MIN_MARKET_CAP_CR:.0f}cr"
        reasons = []
        if self.has_positive_catalyst:
            reasons.append("positive catalyst")
        if self.bulk_deal_flag:
            reasons.append("bulk deal")
        if self.fundamental_score is None and self.technical_score is None and self.market_cap_cr is None:
            reasons.append("no fund/tech data available — floor check skipped")
        return True, ", ".join(reasons) if reasons else "no red flags"


async def _fetch_fund_tech(client: httpx.AsyncClient, symbol: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    fund_score = market_cap_cr = tech_score = None
    try:
        r = await client.get(f"{config.FUNDAMENTAL_URL}/analyze/{symbol}", timeout=config.QUALITY_GATE_TIMEOUT_S)
        if r.status_code == 200:
            fj = r.json()
            fund_score = fj.get("fundamental_score")
            raw_mcap = fj.get("market_cap") or (fj.get("raw") or {}).get("market_cap")
            if raw_mcap:
                try:
                    market_cap_cr = float(raw_mcap) / 1e7  # rupees -> crore
                except (TypeError, ValueError):
                    market_cap_cr = None
    except Exception as e:
        logger.info("quality_gate: fundamental fetch failed for %s (%s)", symbol, e)

    try:
        r = await client.get(f"{config.TECHNICAL_URL}/analyze/{symbol}", timeout=config.QUALITY_GATE_TIMEOUT_S)
        if r.status_code == 200:
            tech_score = r.json().get("technical_score")
    except Exception as e:
        logger.info("quality_gate: technical fetch failed for %s (%s)", symbol, e)

    return fund_score, tech_score, market_cap_cr


async def _fetch_event_signal(client: httpx.AsyncClient, symbol: str) -> tuple[Optional[bool], Optional[float], bool]:
    has_catalyst = event_score = None
    bulk_flag = False
    try:
        r = await client.get(f"{config.EVENT_URL}/events/{symbol}/categorized", timeout=config.QUALITY_GATE_TIMEOUT_S)
        if r.status_code == 200:
            ej = r.json()
            has_catalyst = ej.get("has_positive_catalyst")
            event_score = ej.get("recent_event_score")
            bulk_flag = bool(ej.get("bulk_deals"))
    except Exception as e:
        logger.info("quality_gate: event fetch failed for %s (%s)", symbol, e)
    return has_catalyst, event_score, bulk_flag


async def check(symbol: str) -> QualitySignal:
    """Run all three best-effort checks concurrently for one symbol.
    Never raises — worst case every field is None/False."""
    if not config.QUALITY_GATE_ENABLED:
        return QualitySignal(symbol=symbol)

    async with httpx.AsyncClient() as client:
        try:
            (fund_score, tech_score, market_cap_cr), (has_catalyst, event_score, bulk_flag) = await asyncio.gather(
                _fetch_fund_tech(client, symbol),
                _fetch_event_signal(client, symbol),
            )
        except Exception as e:
            # Belt-and-suspenders — asyncio.gather itself shouldn't raise given
            # the inner functions already swallow their own exceptions, but a
            # quality signal must never be able to crash the trading loop.
            logger.error("quality_gate: unexpected error for %s: %s", symbol, e, exc_info=True)
            return QualitySignal(symbol=symbol)

    return QualitySignal(
        symbol=symbol,
        fundamental_score=fund_score,
        technical_score=tech_score,
        market_cap_cr=market_cap_cr,
        has_positive_catalyst=has_catalyst,
        recent_event_score=event_score,
        bulk_deal_flag=bulk_flag,
    )
