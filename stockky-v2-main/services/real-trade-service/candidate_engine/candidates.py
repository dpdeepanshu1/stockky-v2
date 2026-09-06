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
from datetime import datetime, timedelta, timezone
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
    # ── Issue 1 fix (Option A) — momentum-breakout track ────────────────────
    # Raw candidate symbols only (no score/decision on this endpoint's
    # payload) — quality gating for this track happens entirely inside
    # _volume_shock_analysis() below, not via a source-provided score.
    "volume_shock": "/scan/universe",
}

# ── Option A (Issue 1 fix) — momentum-breakout track thresholds ─────────────
# Real backtest against the 30-Aug-2026 NSE "volume shocker" session showed
# the standard _multi_tf_analysis() waterfall above (6m_downtrend +
# weighted-MTF, calibrated for a choppy/weak large-cap market) is — by
# design — very good at rejecting exactly the single/multi-day breakout move
# that most real volume-shocker days consist of: even using the day's own
# post-move close, only 3/17 backtested movers passed. Rather than loosen
# the existing, presumably-tested conservative filter for every candidate
# (Option B), this adds a second, fully independent track that skips
# 6m_downtrend/weak_MTF entirely and instead requires a genuine volume shock
# (today's volume far above its 20-day average) plus a genuine breakout
# return. It still enforces the two checks that are about position safety
# rather than a market-view judgment (price floor, ATR cap) — those aren't
# opinions about market regime, they protect against bad fills regardless of
# strategy. The liquidity floor is enforced later anyway, downstream, by
# risk_engine's own hard_floor_liquidity check at order time, so it isn't
# duplicated here. See STOCKKY_ISSUE_LOG_AND_FIXES.md Issue 1 for the full
# backtest evidence and the two alternative options that were not taken.
#
# ── 30-Aug-2026 CALIBRATION (real NSE bhavcopy, 819,906 rows, 1 year) ───────
# Multiplier 2.0 captures all Groww screenshot movers including borderline
# ones (NAZARA 2.67x, KAPSTON 2.63x). Return gate does the heavy filtering.
#
# DELIVERY % FINDINGS (1-year backtest, vol>=5x, ret>=5%):
#   <30% delivery: n=3720, win=47.4%, mean=+0.57% next day
#   30-60% delivery: n=1575, win=49.3%, mean=+0.75%
#   >60% delivery: n=288,  win=51.0%, mean=+1.33%
# → Higher delivery = real institutional buying, not intraday churn
#
# HIGH CONVICTION TIER (new, 30-Aug-2026 calibration):
#   vol >= HIGH_CONVICTION_VOL_MULTIPLIER AND return >= HIGH_CONVICTION_MIN_RETURN_PCT
#   Backtest: n=553, win=55.7%, mean=+2.28% next day vs 48.1% base
#   Examples from 28-Aug-2026: MASTEK(73x, 18%), IKIO(36x, 15%), TEJASNET(39x, 8%)
#
# 2-DAY DECAY FINDING:
#   Day+1 mean return for high-conviction movers = +2.28%
#   Day+2 mean return = -1.30%  → time_stop = EOD+1 recommended (added to payload)
#
# UPPER CIRCUIT (>=19.9%) FINDING:
#   n=509, win=69.7%, mean=+5.22% next day — the STRONGEST signal in the dataset.
#   These are tagged high_conviction=True + upper_circuit=True in payload.
# BUG FIX (31-Aug-2026): both candidate refresh loops below used to fire one
# asyncio.Task PER symbol with no concurrency cap at all — with a ~500-symbol
# scan universe and up to 8 HTTP calls per symbol (7 timeframes + quote on
# the standard track), that is thousands of simultaneous requests slammed
# into market-data-service in one burst. market-data-service itself throttles
# yfinance to YFINANCE_MAX_CONCURRENT=6 concurrent calls (see
# docker-compose.yml) — everything past the front of that queue sits waiting
# and blows past this file's own 8s/12s per-request timeouts before
# market-data-service ever gets to it. That is what produced the
# "No quote available for volume-shock check" rejection on literally every
# symbol in one cycle, RELIANCE/HDFCBANK/INFY included — those are never
# actually unquotable, the fetch never had a chance to complete. Bounding
# concurrency here keeps the in-flight request count sane so the fast ones
# succeed instead of all of them queuing behind each other into a timeout.
CANDIDATE_ANALYSIS_CONCURRENCY = int(os.getenv("CANDIDATE_ANALYSIS_CONCURRENCY", "15"))

# 2026-09-01 tuning: loosened from 2.0x/5.0% to widen the entry gate so more
# of /scan/universe's momentum_movers survive into the candidate list. This
# trades precision for volume — the 30-Aug/re-backtest numbers in the block
# above were measured at 2.0x/5.0%, not at these looser values, so treat the
# base-tier win-rate/mean-return figures here as directional, not exact,
# until re-backtested at the new cutoffs. HIGH_CONVICTION/UPPER_CIRCUIT
# tiers below are left untouched — those are the strong, well-evidenced
# signals and loosening them would dilute the one part of this gate with
# the most backtest support. Both remain env-overridable with no code
# change if this turns out too loose (or not loose enough) in practice.
VOLUME_SHOCK_MULTIPLIER = float(os.getenv("CANDIDATE_VOLUME_SHOCK_MULTIPLIER", "1.5"))
# 2026-09-03 recalibration: lowered from 3.5% → 2.5%.
# Rationale: real NSE volume-shocker sessions (Groww screenshots 2026-09-03)
# show genuine institutional movers like Hikal +16.95%, Raymond +14.13%,
# GOCL +9.46%, Elecon +7.42%, Shanthi Gears +9.99% — all well above 2.5%.
# The 3.5% gate was already rejecting today's entire scan universe (all 8
# symbols scored 0.9%–2.8%), meaning no volume-shock candidates were ever
# inserted. Lowering to 2.5% lets moderate-strength movers through while
# the HIGH_CONVICTION (15x vol + 15% return) and UPPER_CIRCUIT (19.9%)
# tiers — which have the strongest backtest evidence — remain unchanged.
VOLUME_SHOCK_MIN_RETURN_PCT = float(os.getenv("CANDIDATE_VOLUME_SHOCK_MIN_RETURN_PCT", "2.5"))

# HIGH CONVICTION tier — from 1-year NSE backtest (30-Aug-2026):
# vol >= 15x AND return >= 15% → 55.7% next-day win rate, mean +2.28%
# Upper circuit (>=19.9%) → 69.7% win rate, mean +5.22% — strongest signal
HIGH_CONVICTION_VOL_MULTIPLIER = float(os.getenv("CANDIDATE_HC_VOL_MULTIPLIER", "15.0"))
HIGH_CONVICTION_MIN_RETURN_PCT = float(os.getenv("CANDIDATE_HC_MIN_RETURN_PCT", "15.0"))
UPPER_CIRCUIT_THRESHOLD_PCT = float(os.getenv("CANDIDATE_UPPER_CIRCUIT_PCT", "19.9"))

# Delivery % quality tiers — from 1-year NSE backtest:
# >60% = institutional quality; include in payload for frontend display
DELIVERY_HIGH_QUALITY_PCT = float(os.getenv("CANDIDATE_DELIVERY_HIGH_QUALITY_PCT", "60.0"))

# ── 2026-09-01 re-backtest (819,906-row NSE bhavcopy, reproduced independently) ──
# Confirmed the tiered structure above holds (win rate rises with each tier,
# 2-day decay confirmed) though exact magnitudes came in a bit lower than the
# original 30-Aug calibration note (base 44.8% vs 48.1%, HC 51.2% vs 55.7%,
# UC 67.4% vs 69.7% — same direction, likely a different data window/methodology
# than whatever produced the original numbers; not a discrepancy worth chasing
# further, the ranking and decay conclusions are what the tiers/time-stop
# actually depend on).
# NEW finding this round: the BASE tier (vol>=2x, ret>=5%, the two above it
# already filter fine) is measurably improved by requiring delivery data to
# be known and >=30% — win 44.8%→45.7%, mean +0.27%→+0.41%, median flips
# from -0.08% to +0.00%. Only applied to the base tier (not HIGH_CONVICTION
# or UPPER_CIRCUIT, whose n is already small and whose win rates are already
# strong on their own). Rejects only when delivery_pct IS available and is
# below this bar — a candidate with no delivery data (get_delivery's neutral
# 50.0 fallback, or a genuine missing read) is NOT rejected on this check
# alone, since a missing-data reject would be indistinguishable from a
# low-delivery reject in the logs and this filter is meant to catch
# intraday-churn moves specifically, not unrelated data gaps.
BASE_TIER_MIN_DELIVERY_PCT = float(os.getenv("CANDIDATE_BASE_MIN_DELIVERY_PCT", "30.0"))


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
            # Same 38s worst-case math as _fetch_quote above (20s rate_limiter
            # max_wait + 18s YFINANCE_HARD_TIMEOUT_SEC) — history goes through
            # the same patched Ticker.history() rate-limiter gate.
            timeout=42.0,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("candles") or []
    except Exception as e:
        logger.debug("history %s/%s failed: %s", symbol, period, e)
    return []


async def _fetch_quote(client: httpx.AsyncClient, symbol: str) -> Optional[dict]:
    try:
        # Timeout math: on a cache miss this falls through to yfinance REST,
        # whose worst case server-side is 20s (rate_limiter.py max_wait) +
        # 18s (YFINANCE_HARD_TIMEOUT_SEC) = 38s. A shorter client timeout
        # would give up before the server's own fail-fast could ever fire.
        # _prefetch_quotes_bulk() above means this is almost always a warm
        # cache hit (fast) — 42s only matters for whatever wasn't warmed.
        r = await client.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=42.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("quote %s failed: %s", symbol, e)
    return None


# ── 2026-09-01 bugfix: delivery_pct was never actually fetched ──────────────
# _volume_shock_analysis used to read quote.get("delivery_pct") /
# quote.get("deliv_per"), but market-data-service's /quote and /quotes/bulk
# responses are built by _pad_quote_response(), whose fixed output schema
# (symbol/name/price/cmp/previous_close/day_change_pct/day_high/day_low/
# volume/atr/market_cap/pe_ratio/source/fetched_at) never includes either
# key — delivery is served from a completely separate GET /delivery/{symbol}
# endpoint (bhavcopy.get_delivery), never merged into the quote payload.
# So delivery_pct was silently always 0 here, which made high_delivery
# always None and the whole BASE_TIER_MIN_DELIVERY_PCT gate (the
# 2026-09-01 re-backtest finding documented above — 44.8%→45.7% win rate
# uplift) dead code: `delivery_pct > 0` was never true, so the reject branch
# could never fire and the payload's delivery_pct/high_delivery fields sent
# to the frontend were always None even when NSE delivery data genuinely
# existed for that symbol. Fixing this by actually calling the delivery
# endpoint below, gated to the base tier only (see call site) since
# /delivery/{symbol} is Redis-cached server-side but can still be a cold
# NSE/bhavcopy fetch — no need to pay that for upper_circuit/high_conviction
# candidates, which never consult delivery_pct for their reject decision.
async def _fetch_delivery(client: httpx.AsyncClient, symbol: str) -> Optional[dict]:
    try:
        r = await client.get(f"{MARKET_DATA_URL}/delivery/{symbol}", timeout=20.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("delivery %s failed: %s", symbol, e)
    return None


# ── 2026-09-01 incident fix: bulk quote pre-warming ──────────────────────────
# candidate_engine used to fire one GET /quote/{symbol} per candidate,
# concurrency-bounded but still one HTTP call (and one competing shot at the
# shared yfinance rate limiter) per symbol. For any symbol outside the
# ~250-name AngelOne/Yahoo live-feed subscription that falls through to
# yfinance REST — 2 req/s sustained, burst 6 (see rate_limiter.py) — so a
# 200+ symbol scan universe produces 200+ requests competing for ~2
# tokens/sec, most of which queue well past this file's own per-request
# timeout and come back empty. That is what caused the 2026-09-01 incident:
# candidate_engine logged "206/206 volume_shock symbols rejected for missing
# quote/history data" — none of those symbols were actually unquotable, the
# fetch just never got a turn.
#
# market-data-service's POST /quotes/bulk was already built to solve exactly
# this class of problem (its own docstring: "Replaces ticker-by-ticker loops
# that trigger free-tier 429 cascades") — ONE yf.download() call per chunk,
# ONE rate-limiter acquisition, and every result gets cached under
# quote:{symbol} server-side. Warming that cache before the per-symbol
# fetches below run means _fetch_quote() mostly hits a warm cache (fast,
# no rate-limiter contention) instead of racing 200 other requests for the
# same few tokens/sec.
BULK_QUOTE_CHUNK_SIZE = int(os.getenv("CANDIDATE_BULK_QUOTE_CHUNK_SIZE", "40"))
BULK_QUOTE_TIMEOUT_SECONDS = float(os.getenv("CANDIDATE_BULK_QUOTE_TIMEOUT_SECONDS", "90.0"))

# 2026-09-01 incident fix (504 / 300+s "Fetching candidates" stall): this
# loop used to `await` one chunk POST at a time in a plain `for`, so N
# chunks cost the SUM of every chunk's time. When Yahoo is having a bad
# day, each chunk's yf.download() pays the full YFINANCE_HARD_TIMEOUT_SEC
# (18s) before failing — a ~330-symbol volume_shock universe at chunk
# size 40 is 9 chunks, i.e. 9 x 18s = 162s minimum sequential, more once
# the rate-limiter's own queuing/backoff is added on top, which is what
# produced the exact 333.9s stall + dashboard 504 in the 2026-09-01
# incident log. Firing chunks concurrently (bounded, so we don't slam
# market-data-service or the shared yfinance bucket any harder than
# before — the token bucket + circuit breaker downstream still cap real
# throughput) turns that into roughly ONE chunk's wall-clock time instead
# of the sum. Paired with rate_limiter.py's new breaker check on the bulk
# yf.download() path, a Yahoo outage now fails the whole prefetch in
# roughly one hard-timeout window, not one per chunk.
BULK_QUOTE_CONCURRENCY = int(os.getenv("CANDIDATE_BULK_QUOTE_CONCURRENCY", "4"))


async def _prefetch_quotes_bulk(client: httpx.AsyncClient, symbols: list[str]) -> None:
    """Best-effort: warm market-data-service's quote cache for `symbols` via
    chunked POST /quotes/bulk calls, fired with bounded concurrency. Never
    raises — if a chunk fails (or /quotes/bulk itself has an issue), the
    per-symbol _fetch_quote() fallback that runs afterward still works
    exactly as it did before this fix, just slower for whichever symbols
    didn't get warmed."""
    unique = list(dict.fromkeys(s for s in symbols if s))
    if not unique:
        return

    chunks = [
        unique[i:i + BULK_QUOTE_CHUNK_SIZE]
        for i in range(0, len(unique), BULK_QUOTE_CHUNK_SIZE)
    ]
    sem = asyncio.Semaphore(BULK_QUOTE_CONCURRENCY)

    async def _post_chunk(chunk: list[str]) -> None:
        async with sem:
            try:
                await client.post(
                    f"{MARKET_DATA_URL}/quotes/bulk",
                    json={"symbols": chunk},
                    timeout=BULK_QUOTE_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.debug(
                    "bulk quote prefetch chunk of %d symbols failed: %s: %s",
                    len(chunk), type(e).__name__, e,
                )

    await asyncio.gather(*(_post_chunk(c) for c in chunks))


# ── Analysis helpers ──────────────────────────────────────────────────────────

_ATR_WINDOW = 14


def _compute_atr_from_candles(candles: list) -> Optional[float]:
    """14-period ATR (simple average of true ranges) from OHLC candle dicts.
    Returns None if there are fewer than _ATR_WINDOW+1 valid candles.
    Used by _multi_tf_analysis and _volume_shock_analysis so both can derive
    ATR from candles they already fetched — no extra HTTP call needed."""
    if not candles or len(candles) < _ATR_WINDOW + 1:
        return None
    try:
        trs = []
        for i in range(1, len(candles)):
            h  = float(candles[i].get("high")  or 0)
            l  = float(candles[i].get("low")   or 0)
            pc = float(candles[i - 1].get("close") or 0)
            if h <= 0 or l <= 0 or pc <= 0:
                continue
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < _ATR_WINDOW:
            return None
        return sum(trs[-_ATR_WINDOW:]) / _ATR_WINDOW
    except Exception:
        return None


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

    # Data-starved marker: quote AND every single timeframe fetch came back
    # empty. Unlike a genuine "weak momentum" rejection (some timeframes
    # returned real numbers, they just weren't bullish), this means nothing
    # came back from market-data-service at all for this symbol — almost
    # always an upstream fetch problem (overload/timeout), not a real
    # candidate-quality signal. Callers use this to distinguish the two
    # instead of guessing from reject_reason text.
    data_starved = quote is None and all(v is None for v in tf_returns.values())
    if data_starved:
        return {
            "reject_reason": "No quote or history available (data fetch failed for all timeframes).",
            "tf_returns": tf_returns, "bullish_count": 0, "atr_pct": None,
            "data_starved": True,
        }

    # ── Check 0: live quote must actually resolve ────────────────────────────
    # Previously, a symbol with a broken/unresolvable quote (bad ticker,
    # ambiguous company-name symbol like "APOLLO" instead of "APOLLOHOSP",
    # transient market-data-service failure) could still pass every check
    # below as long as SOME history timeframe came back — current_price
    # just silently sat at 0 and none of the price-based checks fired. The
    # candidate got inserted anyway, then sat forever as "WAIT — No current
    # price available" every cycle (entry_engine hits the same broken
    # /quote lookup and can never price it), permanently cluttering the
    # watchlist. Reject it here instead, once, with a reason that actually
    # explains why.
    if current_price <= 0:
        return {
            "reject_reason": (
                "No live quote available for this symbol — market-data-service "
                "couldn't resolve a price (bad/ambiguous ticker, delisted, or a "
                "transient upstream failure). Skipping rather than inserting a "
                "candidate that can never be priced."
            ),
            "tf_returns": tf_returns, "bullish_count": 0, "atr_pct": None,
        }

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
    # ATR is computed from the 1m (1mo/1d) candles already fetched above —
    # quote.get("atr") was always None because market-data-service's /quote
    # endpoint never computes or returns ATR, and the AngelOne tick feed
    # carries no history.  Computing from candles in-hand costs nothing extra.
    atr_pct: Optional[float] = None
    candles_1m_for_atr = fetched.get("1m")
    if isinstance(candles_1m_for_atr, list) and current_price > 0:
        atr_val = _compute_atr_from_candles(candles_1m_for_atr)
        if atr_val and atr_val > 0:
            atr_pct = round(atr_val / current_price * 100, 2)
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
    # 2026-09-02 fix: /surprise/ipo/list wraps results under "results", not "items"
    # (verified against api-gateway/ipo_scanner.py's get_ipo_list return value).
    # Checking "results" first, then "items" as a fallback for schema changes.
    items = payload if isinstance(payload, list) else (
        (payload or {}).get("results") or (payload or {}).get("items") or []
    )
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


def _rows_from_volume_shock(payload: Any) -> list[dict]:
    """Option A (Issue 1 fix) — momentum-breakout track raw candidate list.

    /scan/universe's `momentum_movers` field is api-gateway's already-computed
    NSE gainers/losers/volume-gainers list (see _get_momentum_movers()).
    There is no conviction score on this payload the way hot_picks/ipo/
    surprise have one, so every row gets a nominal placeholder score here —
    the REAL quality gate for this track is _volume_shock_analysis()'s
    volume-shock + return-shock checks, run later, not this score."""
    if not isinstance(payload, dict):
        return []
    symbols = payload.get("momentum_movers") or []
    out = []
    for sym in symbols:
        if not sym or not isinstance(sym, str):
            continue
        clean = sym.upper().strip()
        if not clean:
            continue
        out.append({
            "symbol":           clean,
            "source_tab":       "volume_shock",
            "decision_label":   "VOLUME_SHOCK",
            "conviction_score": MIN_CONVICTION,  # nominal — see docstring
            "signal_price":     None,
            "raw_payload":      {"symbol": clean, "source": "momentum_movers"},
        })
    return out


async def _volume_shock_analysis(client: httpx.AsyncClient, symbol: str) -> dict:
    """
    Option A (Issue 1 fix) — momentum-breakout quality gate.

    Deliberately does NOT run _multi_tf_analysis()'s 6m_downtrend or
    weighted-MTF checks — those are exactly what the 30-Aug-2026 backtest
    showed reject most real single/multi-day volume-shocker moves. Instead
    requires a genuine volume shock (today's volume >= VOLUME_SHOCK_MULTIPLIER
    x the 20-day average — a real volume-shock definition, distinct from the
    standard track's 5d-vs-20d "volume health" ratio) AND a genuine breakout
    return (today's return >= VOLUME_SHOCK_MIN_RETURN_PCT). Still enforces
    the position-safety checks that apply regardless of market view: minimum
    price floor and ATR volatility cap (same thresholds as the standard
    track). Liquidity is enforced downstream by risk_engine's own hard
    liquidity floor at order time, so it is not duplicated here.
    """
    quote_task = asyncio.create_task(_fetch_quote(client, symbol))
    hist_task = asyncio.create_task(_fetch_history(client, symbol, "1mo", "1d"))
    quote, candles = await asyncio.gather(quote_task, hist_task, return_exceptions=True)

    if isinstance(quote, Exception) or not quote:
        return {"reject_reason": "No quote available for volume-shock check.", "atr_pct": None}
    if isinstance(candles, Exception) or not isinstance(candles, list) or len(candles) < 6:
        return {"reject_reason": "Insufficient daily history for volume-shock check.", "atr_pct": None}

    current_price = float(quote.get("price") or quote.get("cmp") or 0)

    # ── quote object came back but with no usable price (edge case: quote
    # dict has other fields but price/cmp are missing/zero) — same failure
    # mode as the standard track, same fix: reject rather than let it
    # through with a phantom price. ─────────────────────────────────────
    if current_price <= 0:
        return {"reject_reason": "Quote returned but no usable price for volume-shock check.", "atr_pct": None}

    # ── Position-safety check: minimum price floor (kept — see module docstring) ──
    if current_price > 0 and current_price < MIN_STOCK_PRICE:
        return {
            "reject_reason": (
                f"Price ₹{current_price:.2f} is below ₹{MIN_STOCK_PRICE} minimum. "
                "Sub-₹20 stocks in India have operator activity risk, "
                "very wide bid-ask spreads, and illiquid exits. Skip."
            ),
            "atr_pct": None,
        }

    # ── Genuine breakout return check ─────────────────────────────────────────
    prior_close = float(candles[-2].get("close") or 0)
    today_close = float(candles[-1].get("close") or current_price or 0)
    if prior_close <= 0 or today_close <= 0:
        return {"reject_reason": "Could not compute today's return for volume-shock check.", "atr_pct": None}
    today_return_pct = round((today_close / prior_close - 1) * 100, 2)
    if today_return_pct < VOLUME_SHOCK_MIN_RETURN_PCT:
        return {
            "reject_reason": (
                f"Today's return {today_return_pct:.1f}% < "
                f"{VOLUME_SHOCK_MIN_RETURN_PCT}% volume-shock breakout threshold."
            ),
            "atr_pct": None,
        }

    # ── Genuine volume-shock check (today's vol vs 20-day average) ────────────
    vols = [float(c.get("volume", 0)) for c in candles if c.get("volume")]
    if len(vols) < 6:
        return {"reject_reason": "Insufficient volume history for volume-shock check.", "atr_pct": None}
    today_vol = vols[-1]
    prior_vols = vols[-21:-1] if len(vols) > 1 else []
    avg20 = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    if avg20 <= 0:
        return {"reject_reason": "No 20-day average volume available for volume-shock check.", "atr_pct": None}
    vol_multiple = today_vol / avg20
    if vol_multiple < VOLUME_SHOCK_MULTIPLIER:
        return {
            "reject_reason": (
                f"Today's volume {vol_multiple:.1f}x 20-day average < "
                f"{VOLUME_SHOCK_MULTIPLIER}x volume-shock threshold."
            ),
            "atr_pct": None,
        }

    # ── Position-safety check: ATR volatility cap (kept — see module docstring) ──
    # Compute ATR from the 1mo/1d candles already fetched by hist_task above.
    # quote.get("atr") was always None (market-data-service /quote never
    # computes or returns ATR; AngelOne tick carries no history), so this is
    # always a real improvement over the prior no-op.
    #
    # 2026-09-01 fix: this used to pass the FULL `candles` list (including
    # today's own shock-day candle) into the 14-day ATR window. A genuine
    # volume-shock move — the exact thing this track is built to catch — by
    # definition produces an unusually wide high/low range on the day it
    # happens, so including it here inflated the very ATR reading meant to
    # measure the stock's *normal* volatility, one contaminated by the event
    # itself. Today's candle carries 1/14 of the trailing-window weight, and
    # for the biggest moves (near-20% upper-circuit days) that was enough to
    # push otherwise-good candidates over the MAX_ATR_PCT cap on the exact
    # day they'd be worth buying (see the 30-Aug backtest note above:
    # upper_circuit has the strongest win rate of any tier). Excluding
    # today's candle (candles[:-1]) measures baseline/pre-shock volatility
    # instead, which is what a "is this normally too wild to size safely"
    # check should be asking. Actual position sizing at order time still
    # uses live price/quantity math, not this pre-shock ATR reading.
    atr_pct: Optional[float] = None
    atr_val = _compute_atr_from_candles(candles[:-1])
    if atr_val and atr_val > 0 and current_price > 0:
        atr_pct = round(atr_val / current_price * 100, 2)
        if atr_pct > MAX_ATR_PCT:
            return {
                "reject_reason": (
                    f"Pre-shock ATR {atr_pct:.1f}% > cap {MAX_ATR_PCT}%. "
                    "Too volatile to produce a safe position size within the "
                    "1% per-trade risk cap."
                ),
                "atr_pct": atr_pct,
            }

    # ── HIGH CONVICTION classification (30-Aug-2026 backtest) ────────────────
    # vol>=15x AND ret>=15%: 55.7% next-day win rate, mean +2.28%
    # Upper circuit (>=19.9%): 69.7% win rate, mean +5.22% — strongest signal
    # 2-day decay: Day+1 mean=+2.28%, Day+2 mean=-1.30% → time_stop hint = EOD+1
    upper_circuit = today_return_pct >= UPPER_CIRCUIT_THRESHOLD_PCT
    high_conviction = (
        upper_circuit or (
            vol_multiple >= HIGH_CONVICTION_VOL_MULTIPLIER
            and today_return_pct >= HIGH_CONVICTION_MIN_RETURN_PCT
        )
    )

    # ── Delivery % context (2026-09-01 bugfix — see _fetch_delivery docstring) ──
    # Only fetched for base-tier candidates: HIGH_CONVICTION/UPPER_CIRCUIT never
    # consult delivery_pct for their reject decision, so there's no reason to
    # pay for the extra (possibly cold-cache) HTTP call on those.
    # get_delivery()'s neutral fallback returns delivery_pct=50.0 with
    # source="fallback_neutral" when no real NSE/bhavcopy data exists for the
    # symbol — that's "unknown", not "known and 50%", so it must NOT be
    # treated as real delivery data here (50.0 is >= BASE_TIER_MIN_DELIVERY_PCT
    # anyway, so treating it as real would silently mask the gap, not just
    # miscount it).
    delivery_pct: Optional[float] = None
    if not upper_circuit and not high_conviction:
        delivery = await _fetch_delivery(client, symbol)
        if isinstance(delivery, dict) and delivery.get("source") != "fallback_neutral":
            raw_dp = delivery.get("delivery_pct")
            if raw_dp is not None:
                delivery_pct = float(raw_dp)
    high_delivery = (
        delivery_pct >= DELIVERY_HIGH_QUALITY_PCT if delivery_pct is not None else None
    )

    # ── BASE-tier delivery quality gate (2026-09-01 re-backtest finding) ──────
    # Only applies when this candidate is NOT already high_conviction/
    # upper_circuit (those tiers already backtest strongly on their own and
    # have small enough n that adding another filter would just shrink them
    # further for no measured benefit) AND delivery data is actually known
    # (a missing-data case is not treated as a low-delivery rejection).
    if (
        not upper_circuit
        and not high_conviction
        and delivery_pct is not None
        and delivery_pct < BASE_TIER_MIN_DELIVERY_PCT
    ):
        return {
            "reject_reason": (
                f"Delivery {delivery_pct:.1f}% < {BASE_TIER_MIN_DELIVERY_PCT}% "
                "for a base-tier (non-high-conviction) volume-shock candidate — "
                "backtest shows this combination is disproportionately "
                "intraday/speculative churn rather than a real breakout."
            ),
            "atr_pct": atr_pct,
        }

    return {
        "reject_reason":     None,
        "today_return_pct":  today_return_pct,
        "vol_multiple":      round(vol_multiple, 2),
        "atr_pct":           atr_pct,
        "current_price":     current_price,
        # ── HIGH CONVICTION fields (30-Aug-2026 NSE backtest) ──────────────
        "high_conviction":   high_conviction,
        "upper_circuit":     upper_circuit,
        "delivery_pct":      delivery_pct,
        "high_delivery":     high_delivery,
        # ── Time-stop hint ──────────────────────────────────────────────────
        # Backtest: Day+1 mean=+2.28%, Day+2 mean=-1.30%.
        # Volume-shock gains evaporate by day 2 — exit by end of next trading day.
        "time_stop_hint":    "EOD+1",
        "backtest_note": (
            "upper_circuit(win=69.7%,mean=+5.22%)" if upper_circuit
            else "high_conviction(win=55.7%,mean=+2.28%)" if high_conviction
            else "vol_shock(win=48.1%,mean=+0.66%)"
        ),
    }


async def _fetch_volume_shock_universe(client: httpx.AsyncClient) -> list[str]:
    """Fetch api-gateway's /scan/universe and pull out its momentum_movers list."""
    payload = await _fetch(client, _SOURCES["volume_shock"])
    return [r["symbol"] for r in _rows_from_volume_shock(payload)]


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

async def _refresh_standard_candidates(db: Session, mode: str, exclude_syms: set) -> tuple[int, set]:
    """The original MANUAL/AUTO candidate flow — untouched by the Issue 1
    fix (see module docstring / _volume_shock_analysis for the new,
    fully-independent track). Returns (inserted_count, handled_symbols) —
    handled_symbols covers every symbol this track saw, whether it was
    inserted or rejected, so the volume_shock phase never re-proposes one
    of them under a different source_tab in the same cycle."""
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
        return 0, set()

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
    seen_symbols = {r["symbol"] for r in rows}

    # Drop symbols already in open positions, or already candidated within
    # the dedupe cooldown — risk_engine would also catch open positions via
    # no_pyramiding, but skipping MTF fetches for them saves time; the
    # cooldown skip is what keeps a still-qualifying symbol from getting a
    # fresh duplicate row every single cycle.
    rows = [r for r in rows if r["symbol"] not in exclude_syms]

    if not rows:
        logger.info(
            "candidate_engine: all %d candidates already have open positions or are in cooldown (mode=%s)",
            len(by_symbol), mode,
        )
        return 0, seen_symbols

    # Run multi-timeframe analysis for all remaining symbols concurrently —
    # one shared client, bounded parallelism (see CANDIDATE_ANALYSIS_CONCURRENCY
    # note above), not one unbounded task per symbol and not sequentially.
    async with httpx.AsyncClient() as client:
        # Warm market-data-service's quote cache in bulk first — see
        # _prefetch_quotes_bulk docstring (2026-09-01 incident fix). This
        # track's row count is normally small (hot_picks/ipo/surprise), but
        # it shares the same per-symbol _fetch_quote() call inside
        # _multi_tf_analysis(), so the same rate-limiter contention applies
        # whenever those sources return a larger batch.
        await _prefetch_quotes_bulk(client, [r["symbol"] for r in rows])

        sem = asyncio.Semaphore(CANDIDATE_ANALYSIS_CONCURRENCY)

        async def _limited_mtf(symbol: str) -> dict:
            async with sem:
                return await _multi_tf_analysis(client, symbol)

        tf_tasks = {
            r["symbol"]: asyncio.create_task(_limited_mtf(r["symbol"]))
            for r in rows
        }
        tf_results = await asyncio.gather(*tf_tasks.values(), return_exceptions=True)
        tf_map: dict[str, dict] = {}
        data_starved_count = 0
        for sym, result in zip(tf_tasks.keys(), tf_results):
            if isinstance(result, Exception):
                logger.warning("MTF analysis error for %s: %s", sym, result)
                tf_map[sym] = {"reject_reason": f"MTF fetch error: {result}", "atr_pct": None}
                data_starved_count += 1
            else:
                tf_map[sym] = result
                if result.get("data_starved"):
                    data_starved_count += 1
        # Diagnostic: if almost every symbol came back with zero data (quote AND
        # every timeframe fetch empty) rather than failing an actual quality
        # check, that's a systemic upstream problem (market-data-service
        # overloaded/unreachable), not hundreds of individually-bad symbols —
        # surface it as one loud line instead of letting it hide inside
        # hundreds of per-symbol "CANDIDATE REJECTED" info logs.
        if tf_tasks and data_starved_count / len(tf_tasks) > 0.5:
            logger.warning(
                "candidate_engine: %d/%d symbols had zero quote/history data "
                "in one cycle (mode=%s) — check market-data-service health/logs, "
                "this almost never means that many quotes are genuinely unavailable.",
                data_starved_count, len(tf_tasks), mode,
            )

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
    return inserted, seen_symbols


async def _refresh_volume_shock_candidates(db: Session, mode: str, exclude_symbols: set) -> int:
    """Option A (Issue 1 fix) — the new, fully-independent momentum-breakout
    track. Never touches _multi_tf_analysis(), the standard rows list, or
    its dedup — a symbol already handled (inserted OR rejected) by the
    standard track this cycle is skipped here so it's never proposed twice
    under two different source_tabs in one cycle."""
    try:
        pstat.set_source(mode, "volume_shock")
    except Exception:
        pass

    async with httpx.AsyncClient() as client:
        candidates = await _fetch_volume_shock_universe(client)
        candidates = [s for s in candidates if s not in exclude_symbols]
        if not candidates:
            return 0

        # 2026-09-01 incident fix — see _prefetch_quotes_bulk docstring.
        # This is the track that actually hit "206/206 volume_shock symbols
        # rejected for missing quote/history data": /scan/universe can
        # return 200+ symbols, most outside the live-feed subscription, so
        # warm the quote cache in bulk BEFORE the per-symbol semaphore loop
        # below fires 200+ individually rate-limited /quote/{symbol} calls.
        await _prefetch_quotes_bulk(client, candidates)

        sem = asyncio.Semaphore(CANDIDATE_ANALYSIS_CONCURRENCY)

        async def _limited_vs(symbol: str) -> dict:
            async with sem:
                return await _volume_shock_analysis(client, symbol)

        vs_tasks = {
            sym: asyncio.create_task(_limited_vs(sym))
            for sym in candidates
        }
        vs_results = await asyncio.gather(*vs_tasks.values(), return_exceptions=True)

    inserted = 0
    skipped  = 0
    no_quote_count = 0
    for sym, result in zip(vs_tasks.keys(), vs_results):
        if isinstance(result, Exception):
            logger.warning("volume_shock analysis error for %s: %s", sym, result)
            skipped += 1
            no_quote_count += 1
            continue

        reject = result.get("reject_reason")
        if reject:
            logger.info(
                "VOLUME_SHOCK CANDIDATE REJECTED %s (mode=%s) | %s", sym, mode, reject[:150]
            )
            skipped += 1
            if "no quote" in reject.lower() or "insufficient" in reject.lower():
                no_quote_count += 1
            continue

        # Elevate conviction_score for high-conviction signals
        # Upper circuit (69.7% win) → score 75; High conviction (55.7% win) → score 65
        # Standard volume shock (48.1% win) → nominal MIN_CONVICTION
        if result.get("upper_circuit"):
            score = max(MIN_CONVICTION, 75.0)
            decision_label = "VOLUME_SHOCK_UPPER_CIRCUIT"
        elif result.get("high_conviction"):
            score = max(MIN_CONVICTION, 65.0)
            decision_label = "VOLUME_SHOCK_HIGH_CONVICTION"
        else:
            score = MIN_CONVICTION
            decision_label = "VOLUME_SHOCK"

        payload = {
            "symbol": sym,
            "source": "momentum_movers",
            "_mtf": {
                "today_return_pct": result.get("today_return_pct"),
                "vol_multiple":     result.get("vol_multiple"),
                "atr_pct":          result.get("atr_pct"),
                "high_conviction":  result.get("high_conviction"),
                "upper_circuit":    result.get("upper_circuit"),
                "delivery_pct":     result.get("delivery_pct"),
                "high_delivery":    result.get("high_delivery"),
                "time_stop_hint":   result.get("time_stop_hint"),
                "backtest_note":    result.get("backtest_note"),
                "market_note":      "Option A momentum-breakout (30-Aug-2026 NSE backtest calibrated)",
            },
        }
        db.add(models.TradeCandidate(
            mode=mode,
            symbol=sym,
            source_tab="volume_shock",
            decision_label=decision_label,
            conviction_score=score,
            signal_price=result.get("current_price"),
            raw_payload=json.dumps(payload),
        ))
        inserted += 1

    if inserted:
        db.commit()

    logger.info(
        "candidate_engine: volume_shock inserted=%d skipped=%d mode=%s",
        inserted, skipped, mode,
    )
    # Same systemic-failure diagnostic as the standard track above — see that
    # note for why this is checked explicitly instead of left to blend into
    # per-symbol REJECTED lines.
    if vs_tasks and no_quote_count / len(vs_tasks) > 0.5:
        logger.warning(
            "candidate_engine: %d/%d volume_shock symbols rejected for missing "
            "quote/history data in one cycle (mode=%s) — check market-data-service "
            "health/logs, this almost never means that many quotes are genuinely "
            "unavailable.",
            no_quote_count, len(vs_tasks), mode,
        )
    return inserted


def _recently_candidated_symbols(db: Session, mode: str, hours: Optional[float] = None) -> set:
    """Symbols that already have a TradeCandidate row (either track, any
    consumed state) inserted within the given cooldown window (defaults to
    CANDIDATE_DEDUPE_COOLDOWN_HOURS=6h for the standard track).

    2026-09-03: volume_shock track uses a shorter 2h cooldown (passed via
    the hours arg). Rationale: volume-shock moves are intraday — the backtest
    shows 2-day decay, but the move itself plays out within hours of the
    signal. A 6h dedupe window means a stock that was regime-blocked at
    9:30am won't re-qualify until 3:30pm (market close). At 2h it can
    re-enter the watchlist later the same session if conditions improve."""
    cooldown = hours if hours is not None else config.CANDIDATE_DEDUPE_COOLDOWN_HOURS
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown)
        rows = (
            db.query(models.TradeCandidate.symbol)
            .filter(models.TradeCandidate.mode == mode,
                    models.TradeCandidate.received_at >= cutoff)
            .all()
        )
        return {r[0] for r in rows}
    except Exception as e:
        logger.debug("_recently_candidated_symbols failed (non-fatal, dedupe skipped): %s", e)
        return set()


async def refresh_candidates(db: Session, mode: str) -> int:
    """
    Fetch every source → score-filter → deduplicate by symbol (keep
    highest conviction) → drop already-open positions and symbols already
    candidated within the cooldown window → run concurrent multi-timeframe
    + quality analysis → insert only passing rows.

    Runs TWO independent tracks (Issue 1 fix, Option A):
      1. The original MANUAL/AUTO flow (_refresh_standard_candidates),
         completely unchanged.
      2. A new momentum-breakout track (_refresh_volume_shock_candidates)
         that skips the 6m_downtrend/weak_MTF checks and instead requires a
         genuine volume + return shock. See module-level comment above
         VOLUME_SHOCK_MULTIPLIER for why.

    Returns the total number of candidate rows inserted across both tracks.
    """
    open_syms = {p.symbol for p in open_positions(db, mode)}
    # Standard track: 6h dedupe cooldown (avoids duplicate cards for slow-moving signals)
    cooldown_syms = _recently_candidated_symbols(db, mode)
    exclude = open_syms | cooldown_syms

    standard_inserted, standard_seen = await _refresh_standard_candidates(db, mode, exclude)

    # Volume-shock track: 2h dedupe cooldown — intraday moves play out within
    # hours; a 6h window would block re-evaluation for the rest of the session
    # if the signal was regime-blocked at open. 2h lets it re-qualify after
    # the regime gate clears (e.g. post-market-score fix deployment today).
    shock_cooldown_syms = _recently_candidated_symbols(db, mode, hours=2.0)
    shock_exclude = open_syms | shock_cooldown_syms | standard_seen
    shock_inserted = await _refresh_volume_shock_candidates(
        db, mode, exclude_symbols=shock_exclude
    )

    return standard_inserted + shock_inserted
