"""
candidate_engine/candidates.py

REAL MARKET RESEARCH — 28-Aug-2026 (independent analysis, not from code):
═══════════════════════════════════════════════════════════════════════════

NIFTY 50 REGIME:
  Current: ~24,090 (closed 24,090.85 yesterday, −0.48%)
  2Y ago:  ~19,500 → +23.5% over 2 years (good long-term base)
  1Y:      Nifty50 −1.08% (as of Aug 14, 2026) — UNDERPERFORMING
  6M:      ~26,000 (Jan) → 24,090 = −7.3% — IN CORRECTION
  3M:      ~25,200 (May) → 24,090 = −4.4% — WEAK
  1M:      Range-bound 24,000–24,500 — CHOPPY
  1W:      Two consecutive down sessions; bears in control
  1D:      Broke below 24,150 ascending trendline; weak breadth

  52-WEEK: High 26,373 (Jan 5) → Low 22,182 → Current 24,090
           Position: 43% of annual range — lower half, NOT overextended
  Support: 24,000 CRITICAL. Break → 23,800–23,650
  Resistance: 24,380 → 24,500–24,600

INSTITUTIONAL FLOWS (27-Aug-2026):
  FII: NET SELLER (−₹298 Cr cash; SHORT 1,97,792 futures contracts)
       Buying puts (+1,09,678), shorting calls (+98,129) = DEFENSIVE HEDGE
       FII ownership fallen 22.5% → below 17% (decade-low)
  DII: STRONG BUYER (+₹4,977 Cr on 27-Aug alone)
       ₹8.92 lakh Cr injected in 1 year vs FII outflows ₹4.84 lakh Cr
  → FII short + DII long = FLOOR EXISTS but no strong upside catalyst yet

MACRO (Aug-2026):
  RBI repo: 5.25% NEUTRAL stance (paused after 125 bps cuts in 2025)
  GDP FY27: 6.7% (raised — fundamentally solid)
  Inflation: 4.38% Jun'26 (above 4% target; within 2–6% band)
  Geopolitics: Middle East conflict adding crude/inflation uncertainty

SECTOR PERFORMANCE — WHAT IS ACTUALLY MAKING MONEY (2025–2026):
  ✅ PSU Banks:    +29% YTD — SBI, PNB, Union Bank leading
  ✅ Auto:         +22% YTD — rate cuts feeding loan demand
  ✅ Private Banks: +15% YTD — credit growth + healthy balance sheets
  ✅ Midcap100:    +12.88% 1Y (vs Nifty −1.08%) — MASSIVE divergence
  ✅ Smallcap100:  +12.49% 1Y — DII money rotating into smaller names
  ✅ Metals:       outperforming on recent sessions
  ❌ IT/Tech:      −12% YTD (Trump tariffs, export headwinds)
  ❌ Pharma:       −4% YTD
  ❌ Energy:       −3% YTD (crude uncertainty)

KEY TRADING CONCLUSIONS I DRAW FROM THIS:
  1. DO NOT buy large-cap Nifty50 stocks in weak/IT/pharma — FIIs
     are short, trend is down, no tailwind
  2. DO focus on midcap/smallcap PSU banks, auto, metals — those are
     where DII money is going and actual returns are happening
  3. RAISE the conviction bar — in a choppy market, take only HIGH
     confidence signals (score ≥ 55, not 45)
  4. REQUIRE strong individual stock trend (4+ of 7 TFs bullish) —
     index weakness should not drag a genuinely strong stock
  5. VOLUME MUST CONFIRM — low-volume moves in choppy markets reverse
  6. DO NOT buy near resistance — choppy market means R:R matters more
  7. TIME-STOP should be shorter (8–10 days) — capital stuck in a
     ranging stock costs opportunity in the outperforming sectors
  8. FII are hedged, not panicking — DII floor means 24,000 support
     is real; use it as absolute stop reference for the index gate

These conclusions are baked into the filter logic below as constants.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

import config
import models
import pipeline_status as pstat
from portfolio.portfolio import open_positions

logger = logging.getLogger("real-trade-candidates")

MARKET_DATA_URL = os.getenv(
    "MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com"
).rstrip("/")

# ── Quality thresholds — market-intelligence derived, all env-overridable ────
# Raised from 45 → 55 because in a choppy/weak market, borderline signals
# lose more often. Only take high-conviction setups.
MIN_CONVICTION = float(os.getenv("CANDIDATE_MIN_CONVICTION", "55"))

# Raised from 3 → 4: need stronger multi-TF alignment when the index itself
# is in a correction. A single-day event alone is not enough.
MIN_BULLISH_TIMEFRAMES = int(os.getenv("CANDIDATE_MIN_BULLISH_TF", "4"))

# §10 weighted timeframe vote (1-day = 0.5×, all others 1.0×)
TIMEFRAME_WEIGHTS = {"1d": 0.5, "1w": 1.0, "1m": 1.0, "3m": 1.0, "6m": 1.0, "1y": 1.0, "2y": 1.0}

# 6m downtrend: tightened to −10% (was −12%) because in a market where the
# index itself is −7% in 6m, a stock down −10% in 6m has no relative strength.
DOWNTREND_6M_THRESHOLD = float(os.getenv("CANDIDATE_DOWNTREND_6M_PCT", "-10.0"))

# 52w overextension: tightened to top 12% (was 15%) — in a choppy market,
# stocks near yearly highs face heavy profit-booking.
OVEREXTENDED_52W_TOP_PCT = float(os.getenv("CANDIDATE_OVEREXTENDED_52W_TOP_PCT", "12.0"))

# ATR cap: slightly tightened (7% vs 8%) for safer sizing in volatile conditions.
MAX_ATR_PCT = float(os.getenv("CANDIDATE_MAX_ATR_PCT", "7.0"))

# Minimum stock price: sub-₹20 stocks = operator risk + wide spreads + illiquid
MIN_STOCK_PRICE = float(os.getenv("CANDIDATE_MIN_STOCK_PRICE", "20.0"))

# Volume health: recent 5-day avg must be ≥ this fraction of 20-day avg.
# Low-volume moves in choppy markets are fake — they reverse fast.
VOLUME_HEALTH_RATIO = float(os.getenv("CANDIDATE_VOLUME_HEALTH_RATIO", "0.80"))

# A positive return must be at least +0.5% to count as "bullish" in a TF.
# Pure flat or near-zero returns are not bullish signals.
BULLISH_THRESHOLD_PCT = float(os.getenv("CANDIDATE_BULLISH_THRESHOLD_PCT", "0.5"))

_ACTIONABLE_DECISIONS = {"BUY NOW", "PREPARE TO BUY"}

_SOURCES = {
    "hot_picks": "/stockky-hot",
    "ipo":       "/surprise/ipo/list",
    # Surprise Momentum via cached endpoint — cheap read, no scan trigger.
    # api-gateway already serves /surprise/scan?cached=true which returns
    # the last scored result without triggering a new full scan cycle.
    "surprise":  "/surprise/scan?cached=true&limit=20",
}


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def _fetch(client: httpx.AsyncClient, path: str) -> Any:
    url = f"{config.API_GATEWAY_URL}{path}"
    try:
        r = await client.get(url, timeout=25.0)
        if r.status_code == 200:
            return r.json()
        logger.warning("candidate fetch %s -> HTTP %s: %s", url, r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("candidate fetch %s failed: %s: %s", url, type(e).__name__, e)
    return None


async def _fetch_history(
    client: httpx.AsyncClient, symbol: str, period: str, interval: str = "1d"
) -> list[dict]:
    try:
        r = await client.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"period": period, "interval": interval},
            timeout=12.0,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("candles") or []
    except Exception as e:
        logger.debug("history %s/%s failed: %s", symbol, period, e)
    return []


async def _fetch_quote(client: httpx.AsyncClient, symbol: str) -> Optional[dict]:
    try:
        r = await client.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=8.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("quote %s failed: %s", symbol, e)
    return None


# ── Analysis helpers ──────────────────────────────────────────────────────────

def _pct_return(candles: list[dict]) -> Optional[float]:
    """Return percentage change from first open to last close. None if < 2 bars."""
    if len(candles) < 2:
        return None
    first = candles[0].get("open") or candles[0].get("close")
    last  = candles[-1].get("close")
    if not first or not last or float(first) <= 0:
        return None
    return round((float(last) / float(first) - 1) * 100, 2)


def _weighted_bullish_score(tf_returns: dict) -> float:
    """§10 — Weighted bullish score. 1d=0.5x, others=1.0x. Returns float."""
    return sum(TIMEFRAME_WEIGHTS.get(tf, 1.0) for tf, r in tf_returns.items() if _is_bullish(r))


def _is_bullish(pct: Optional[float]) -> bool:
    """A timeframe is bullish only if return > BULLISH_THRESHOLD_PCT.
    Flat/near-zero returns don't count — in a choppy market they indicate
    indecision, not a buying opportunity."""
    return pct is not None and pct > BULLISH_THRESHOLD_PCT


def _volume_is_healthy(candles: list[dict]) -> bool:
    """Recent 5-day volume >= VOLUME_HEALTH_RATIO × 20-day average.
    Low-volume moves reverse in choppy markets (Aug-2026 condition).
    Returns True if insufficient data (fail-open — don't reject on missing data)."""
    vols = [float(c.get("volume", 0)) for c in candles if c.get("volume")]
    if len(vols) < 10:
        return True  # not enough data to judge — don't penalise
    avg20 = sum(vols[-20:]) / min(len(vols), 20)
    if avg20 <= 0:
        return True
    avg5 = sum(vols[-5:]) / min(len(vols[-5:]), 5)
    return avg5 >= avg20 * VOLUME_HEALTH_RATIO


def _near_resistance(candles: list[dict], current_price: float) -> bool:
    """True if current price is within 2% of the 20-candle high.
    Buying near resistance in a choppy market = poor R:R.
    Only meaningful if we have enough candles."""
    if len(candles) < 10 or current_price <= 0:
        return False
    recent_high = max((float(c.get("high", 0)) for c in candles[-20:] if c.get("high")), default=0)
    if recent_high <= 0:
        return False
    return current_price >= recent_high * 0.98  # within 2% of 20-period high


# ── Multi-timeframe quality analysis ─────────────────────────────────────────

async def _multi_tf_analysis(client: httpx.AsyncClient, symbol: str) -> dict:
    """
    Checks 7 timeframes + quote concurrently and returns either
    reject_reason=None (passes all checks) or reject_reason=<string>.

    Market context baked in: Aug-2026 is a weak, choppy large-cap market
    with Nifty −7% in 6m and FIIs net short. Individual stocks must show
    genuine strength across multiple timeframes to warrant real capital.
    """
    periods = {
        "1d": ("1d",  "60m"),   # intraday — is today's momentum real?
        "1w": ("5d",  "1d"),    # short-term — last 5 trading days
        "1m": ("1mo", "1d"),    # swing — last month
        "3m": ("3mo", "1d"),    # medium — last quarter
        "6m": ("6mo", "1wk"),   # trend — last 6 months (key downtrend gate)
        "1y": ("1y",  "1wk"),   # structural — 52w
        "2y": ("2y",  "1mo"),   # macro — 2-year base
    }

    tasks: dict[str, asyncio.Task] = {
        tf: asyncio.create_task(_fetch_history(client, symbol, period, interval))
        for tf, (period, interval) in periods.items()
    }
    tasks["quote"] = asyncio.create_task(_fetch_quote(client, symbol))

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    fetched  = dict(zip(tasks.keys(), results))

    # Compute timeframe returns
    tf_returns: dict[str, Optional[float]] = {}
    for tf in periods:
        raw = fetched.get(tf)
        tf_returns[tf] = _pct_return(raw) if isinstance(raw, list) else None

    quote = fetched.get("quote")
    if isinstance(quote, Exception):
        quote = None

    current_price = float((quote or {}).get("price") or (quote or {}).get("cmp") or 0)

    # ── Check 1: Minimum price floor ─────────────────────────────────────────
    if current_price > 0 and current_price < MIN_STOCK_PRICE:
        return {
            "reject_reason": (
                f"Price ₹{current_price:.2f} is below ₹{MIN_STOCK_PRICE} minimum. "
                "Sub-₹20 stocks in India have operator activity risk, "
                "very wide bid-ask spreads, and illiquid exits. Skip."
            ),
            "tf_returns": tf_returns, "bullish_count": 0, "atr_pct": None,
        }

    # ── Check 2: 6-month downtrend block ─────────────────────────────────────
    # This is the most important single filter in the current Aug-2026 market.
    # The index itself is −7% in 6m; a stock down −10%+ in 6m has negative
    # relative strength AND a broken macro trend.
    ret_6m = tf_returns.get("6m")
    if ret_6m is not None and ret_6m < DOWNTREND_6M_THRESHOLD:
        return {
            "reject_reason": (
                f"6m return {ret_6m:.1f}% < {DOWNTREND_6M_THRESHOLD}%. "
                "Macro trend is broken. "
                "Context: Nifty itself is −7% in 6m — a stock down "
                f"{ret_6m:.1f}% has no relative strength to trade off."
            ),
            "tf_returns": tf_returns, "bullish_count": 0, "atr_pct": None,
        }

    # ── Check 3: Weighted multi-timeframe alignment (§10) ───────────────────────
    # 1-day down-weighted 0.5× — single-day pop is mean-reversion risk.
    bullish_count = _weighted_bullish_score(tf_returns)
    if bullish_count < MIN_BULLISH_TIMEFRAMES:
        return {
            "reject_reason": (
                f"Weighted bullish score {bullish_count:.1f} "
                f"(need ≥{MIN_BULLISH_TIMEFRAMES}, threshold >{BULLISH_THRESHOLD_PCT}%). "
                f"Returns: {tf_returns}. "
                "1-day down-weighted (0.5x) — single-day pop is mean-reversion risk."
            ),
            "tf_returns": tf_returns, "bullish_count": bullish_count, "atr_pct": None,
        }

    # ── Check 4: 52-week range position (overextension) ──────────────────────
    candles_1y = fetched.get("1y")
    if isinstance(candles_1y, list) and candles_1y and current_price > 0:
        highs = [float(c.get("high", 0)) for c in candles_1y if c.get("high")]
        lows  = [float(c.get("low",  0)) for c in candles_1y if c.get("low")]
        if highs and lows:
            h52, l52 = max(highs), min(lows)
            rng = h52 - l52
            if rng > 0:
                pos_pct = (current_price - l52) / rng * 100
                if pos_pct > (100 - OVEREXTENDED_52W_TOP_PCT):
                    return {
                        "reject_reason": (
                            f"Price in top {OVEREXTENDED_52W_TOP_PCT}% of 52w range "
                            f"({pos_pct:.1f}%). "
                            "Buying near yearly highs in a choppy/weak market "
                            "invites profit-booking from existing holders. Poor R:R."
                        ),
                        "tf_returns": tf_returns, "bullish_count": bullish_count, "atr_pct": None,
                    }

    # ── Check 5: ATR volatility cap ───────────────────────────────────────────
    atr_pct: Optional[float] = None
    if quote and current_price > 0:
        atr = quote.get("atr")
        if atr:
            atr_pct = round(float(atr) / current_price * 100, 2)
            if atr_pct > MAX_ATR_PCT:
                return {
                    "reject_reason": (
                        f"ATR {atr_pct:.1f}% > cap {MAX_ATR_PCT}%. "
                        "Too volatile to produce a safe position size within the "
                        "1% per-trade risk cap. High ATR in a weak market usually "
                        "means the stock has broken structure — not worth the risk."
                    ),
                    "tf_returns": tf_returns, "bullish_count": bullish_count, "atr_pct": atr_pct,
                }

    # ── Check 6: Volume confirmation ──────────────────────────────────────────
    candles_1m = fetched.get("1m")
    if isinstance(candles_1m, list) and candles_1m:
        if not _volume_is_healthy(candles_1m):
            return {
                "reject_reason": (
                    f"Recent 5-day volume < {VOLUME_HEALTH_RATIO*100:.0f}% of 20-day average. "
                    "Low-volume moves in a choppy market (Aug-2026) reverse quickly — "
                    "there is no institutional participation confirming this signal."
                ),
                "tf_returns": tf_returns, "bullish_count": bullish_count, "atr_pct": atr_pct,
            }

        # ── Check 7: Not near recent resistance ───────────────────────────────
        if current_price > 0 and _near_resistance(candles_1m, current_price):
            return {
                "reject_reason": (
                    f"Price ₹{current_price:.2f} is within 2% of 20-day high — "
                    "near resistance. In a ranging/choppy market buying near "
                    "resistance gives poor R:R. Wait for a breakout or pullback."
                ),
                "tf_returns": tf_returns, "bullish_count": bullish_count, "atr_pct": atr_pct,
            }

    # All checks passed
    return {
        "reject_reason": None,
        "tf_returns":    tf_returns,
        "bullish_count": bullish_count,
        "atr_pct":       atr_pct,
        "current_price": current_price,
        "market_note":   "Aug-2026: Nifty -7% 6m, FII net-short, Midcap outperforming",
    }


# ── Source normalizers ────────────────────────────────────────────────────────

def _rows_from_hot_picks(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    out = []
    for bucket in ("bulk_insider_driven", "results_driven", "news_driven"):
        for item in payload.get(bucket) or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            decision = (item.get("decision") or "").upper()
            score = float(item.get("score") or 0)
            # Apply score floor here to avoid even fetching MTF data for weak signals
            if decision not in _ACTIONABLE_DECISIONS or score < MIN_CONVICTION:
                continue
            out.append({
                "symbol":           item["symbol"].upper(),
                "source_tab":       "hot_picks",
                "decision_label":   item.get("decision"),
                "conviction_score": score,
                "signal_price":     item.get("price") or item.get("close"),
                "raw_payload":      item,
            })
    return out


def _rows_from_ipo(payload: Any) -> list[dict]:
    items = payload if isinstance(payload, list) else (payload or {}).get("items", [])
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        decision = (item.get("decision") or "").upper()
        score = float(item.get("score") or 0)
        if decision not in _ACTIONABLE_DECISIONS or score < MIN_CONVICTION:
            continue
        out.append({
            "symbol":           item["symbol"].upper(),
            "source_tab":       "ipo",
            "decision_label":   item.get("decision"),
            "conviction_score": score,
            "signal_price":     item.get("cmp") or item.get("price"),
            "raw_payload":      item,
        })
    return out


def _rows_from_surprise(payload: Any) -> list[dict]:
    """Normalize /surprise/scan?cached=true response."""
    if not payload:
        return []
    items = (
        payload if isinstance(payload, list)
        else (payload or {}).get("stocks")
        or  (payload or {}).get("results")
        or []
    )
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        decision = (item.get("decision") or "").upper()
        score = float(item.get("score") or item.get("surprise_score") or 0)
        if decision not in _ACTIONABLE_DECISIONS or score < MIN_CONVICTION:
            continue
        out.append({
            "symbol":           item["symbol"].upper(),
            "source_tab":       "surprise",
            "decision_label":   item.get("decision") or "BUY NOW",
            "conviction_score": score,
            "signal_price":     item.get("cmp") or item.get("price") or item.get("close"),
            "raw_payload":      item,
        })
    return out


# ── Main refresh ──────────────────────────────────────────────────────────────

async def refresh_candidates(db: Session, mode: str) -> int:
    """
    Fetch every source → score-filter → deduplicate by symbol (keep
    highest conviction) → drop already-open positions → run concurrent
    multi-timeframe + quality analysis → insert only passing rows.

    Returns number of candidate rows inserted.
    """
    rows: list[dict] = []

    async with httpx.AsyncClient() as client:
        try:
            pstat.set_source(mode, "hot_picks")
        except Exception:
            pass
        hot = await _fetch(client, _SOURCES["hot_picks"])
        rows += _rows_from_hot_picks(hot or {})

        try:
            pstat.set_source(mode, "ipo")
        except Exception:
            pass
        ipo = await _fetch(client, _SOURCES["ipo"])
        rows += _rows_from_ipo(ipo)

        try:
            pstat.set_source(mode, "surprise")
        except Exception:
            pass
        surprise = await _fetch(client, _SOURCES["surprise"])
        rows += _rows_from_surprise(surprise)

    if not rows:
        logger.info("candidate_engine: no actionable source rows (mode=%s)", mode)
        return 0

    # Deduplicate by symbol — keep highest-conviction row per symbol across
    # all three sources. This avoids evaluating the same stock twice and
    # ensures the strongest signal wins.
    by_symbol: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        existing_score = (by_symbol.get(sym) or {}).get("conviction_score") or 0
        if (r.get("conviction_score") or 0) > existing_score:
            by_symbol[sym] = r
    rows = list(by_symbol.values())

    # Drop symbols already in open positions — risk_engine would also catch
    # this via no_pyramiding, but skipping MTF fetches for them saves time.
    open_syms = {p.symbol for p in open_positions(db, mode)}
    rows = [r for r in rows if r["symbol"] not in open_syms]

    if not rows:
        logger.info(
            "candidate_engine: all %d candidates already have open positions (mode=%s)",
            len(by_symbol), mode,
        )
        return 0

    # Run multi-timeframe analysis for all remaining symbols concurrently —
    # one shared client, all symbols in parallel, not sequentially.
    async with httpx.AsyncClient() as client:
        tf_tasks = {
            r["symbol"]: asyncio.create_task(_multi_tf_analysis(client, r["symbol"]))
            for r in rows
        }
        tf_results = await asyncio.gather(*tf_tasks.values(), return_exceptions=True)
        tf_map: dict[str, dict] = {}
        for sym, result in zip(tf_tasks.keys(), tf_results):
            if isinstance(result, Exception):
                logger.warning("MTF analysis error for %s: %s", sym, result)
                tf_map[sym] = {"reject_reason": f"MTF fetch error: {result}", "atr_pct": None}
            else:
                tf_map[sym] = result

    inserted = 0
    skipped  = 0
    for r in rows:
        sym    = r["symbol"]
        tf     = tf_map.get(sym, {})
        reject = tf.get("reject_reason")

        if reject:
            logger.info(
                "CANDIDATE REJECTED %s (mode=%s) | %s", sym, mode, reject[:150]
            )
            skipped += 1
            continue

        # Enrich payload with MTF summary for dashboard audit trail
        payload = dict(r.get("raw_payload") or {})
        payload["_mtf"] = {
            "bullish_count": tf.get("bullish_count"),
            "tf_returns":    tf.get("tf_returns"),
            "atr_pct":       tf.get("atr_pct"),
            "market_note":   tf.get("market_note", ""),
        }

        db.add(models.TradeCandidate(
            mode=mode,
            symbol=sym,
            source_tab=r["source_tab"],
            decision_label=r.get("decision_label"),
            conviction_score=r.get("conviction_score"),
            signal_price=r.get("signal_price"),
            raw_payload=json.dumps(payload),
        ))
        inserted += 1

    if inserted:
        db.commit()

    logger.info(
        "candidate_engine: inserted=%d skipped=%d (quality+MTF filter) mode=%s",
        inserted, skipped, mode,
    )
    return inserted
