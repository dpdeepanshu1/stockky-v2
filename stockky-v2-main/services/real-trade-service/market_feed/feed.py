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
import threading as _threading
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

logger = logging.getLogger("real-trade-market-feed")

# Mirrors every other Stockky service's MARKET_DATA_URL env convention
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")

# §1 — max age for live_quotes rows before we consider them stale.
# If the AngelOne row is older than this, fall through to the yfinance path.
LIVE_QUOTE_MAX_AGE_S = float(os.getenv("LIVE_QUOTE_MAX_AGE_S", "5.0"))

# §2 — Process-scoped ATR cache.
#
# Root cause of the "ATR silent null" bug:
#   The AngelOne/Yahoo WS fast path (Source 1) returns price+volume from a
#   live tick, but a tick carries no ATR — ATR requires 14 candles of OHLC
#   history.  The code previously wrote atr=None unconditionally on every
#   AngelOne hit.  During market hours the AngelOne path is the common path
#   (fresh tick ≤ 5s almost always wins), so entry_engine._atr_stop_target_pct()
#   received None on virtually every real trade and fell back to the fixed
#   MIN_STOP_PCT / MIN_TARGET_PCT constants — ATR-adaptive sizing was
#   completely inactive in production.
#
#   Even Source 2 (market-data-service /quote) never returns ATR: the
#   QuoteResponse schema has no atr field and _yahoo_ohlcv_quote() doesn't
#   compute one, so q.get("atr") was also always None on that path.
#
# Fix:
#   We maintain a lightweight per-process dict: clean_symbol → float ATR.
#   Whenever we fall through to Source 2 (yfinance path), we fire a
#   non-blocking async history fetch, compute a 14-period ATR from the
#   candles, and store it here.  Subsequent calls — whether via AngelOne or
#   yfinance — serve the cached ATR.
#
#   Staleness is acceptable: ATR is a 14-day rolling metric; intraday drift
#   is small (a few paise on a ₹500 stock), and stop/target sizing is not
#   sensitive to that level of precision.  The cache resets on service
#   restart, which is fine — it re-warms within the first evaluation cycle.
#
#   The history fetch (8s timeout) runs as a background task concurrent with
#   the quote return so it adds zero latency to the hot path.

_ATR_CACHE: dict[str, float] = {}
_ATR_LOCK = _threading.Lock()
ATR_WINDOW = 14
_ATR_HISTORY_PERIOD = os.getenv("FEED_ATR_HISTORY_PERIOD", "1mo")  # 14+ daily candles


def _compute_atr_from_candles(candles: list) -> Optional[float]:
    """14-period ATR (simple average of true ranges) from OHLC candle dicts.
    Returns None if there are fewer than ATR_WINDOW+1 valid candles."""
    if not candles or len(candles) < ATR_WINDOW + 1:
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
        if len(trs) < ATR_WINDOW:
            return None
        return sum(trs[-ATR_WINDOW:]) / ATR_WINDOW
    except Exception:
        return None


def _store_atr(symbol: str, atr: float) -> None:
    clean = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if clean and atr > 0:
        with _ATR_LOCK:
            _ATR_CACHE[clean] = atr


def _cached_atr(symbol: str) -> Optional[float]:
    """Return the most-recently-computed ATR for this symbol, or None."""
    clean = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with _ATR_LOCK:
        return _ATR_CACHE.get(clean)


async def _bg_refresh_atr(client: httpx.AsyncClient, symbol: str) -> None:
    """Background task: fetch 1mo/1d history, compute ATR, store in cache.
    Non-blocking and non-raising — called with asyncio.create_task()."""
    try:
        r = await client.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"period": _ATR_HISTORY_PERIOD, "interval": "1d"},
            timeout=8.0,
        )
        if r.status_code == 200:
            candles = (r.json() or {}).get("candles") or []
            atr = _compute_atr_from_candles(candles)
            if atr:
                _store_atr(symbol, atr)
                logger.debug("ATR cache updated: %s → %.4f", symbol, atr)
    except Exception as e:
        logger.debug("_bg_refresh_atr(%s) failed (non-fatal): %s", symbol, e)


class Tick:
    __slots__ = ("symbol", "price", "as_of", "atr", "source", "volume")

    def __init__(self, symbol: str, price: float, as_of: datetime, atr: Optional[float], source: str,
                 volume: Optional[int] = None):
        self.symbol = symbol
        self.price  = price
        self.as_of  = as_of
        self.atr    = atr
        self.source = source
        # Traded volume for the session, when the upstream source provides it.
        # entry_engine/entry.py reads this (via getattr fallback) to compute
        # avg_traded_value for risk_engine's liquidity floor check (#7) — that
        # check silently no-ops without it, so keep this populated wherever a
        # quote source actually returns volume.
        self.volume = volume


async def get_quote(client: httpx.AsyncClient, symbol: str) -> Optional[Tick]:
    """
    §1 — Quote with live_quotes-first cascade:
      1. live_quotes table (AngelOne WS feed, freshness ≤ LIVE_QUOTE_MAX_AGE_S)
         → price from tick, ATR from _cached_atr(); fires _bg_refresh_atr if
           cache is cold so the next call will have a warm value.
      2. market-data-service /quote/{symbol} (yfinance-backed)
         → ATR from _cached_atr() (populated by a concurrent background history
           fetch fired on this call).

    The staleness guard: if the live_quotes row is older than LIVE_QUOTE_MAX_AGE_S
    seconds, treat it as stale and fall through to source 2. This blocks trading
    on frozen data rather than acting on a stale AngelOne tick.
    """
    # ── Source 1: live_quotes table (AngelOne / Yahoo WS) ─────────────────────
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
                    vol = lq.get("volume")

                    # ATR fix: serve from cache; if cold, schedule a background
                    # refresh so the next evaluation cycle has a warm value.
                    atr = _cached_atr(symbol)
                    if atr is None:
                        # Fire-and-forget — doesn't delay this return at all.
                        asyncio.create_task(_bg_refresh_atr(client, symbol))
                        logger.debug(
                            "ATR cache cold for %s (AngelOne hit) — background refresh scheduled",
                            symbol,
                        )

                    return Tick(
                        symbol=symbol,
                        price=float(ltp),
                        as_of=updated_at,
                        atr=atr,   # None on first cycle; populated from next cycle onward
                        source=f"live_quotes({lq.get('source', 'angelone')})",
                        volume=int(vol) if vol not in (None, "") else None,
                    )
                else:
                    logger.debug(
                        "live_quotes %s is %.1fs old > %.1fs limit — falling through",
                        symbol, age_s, LIVE_QUOTE_MAX_AGE_S,
                    )
    except Exception as e:
        logger.debug("live_quotes read failed for %s (non-fatal): %s", symbol, e)

    # ── Source 2: market-data-service /quote (yfinance-backed) ────────────────
    # Also fires a background ATR refresh (non-blocking) so the cache warms
    # concurrently with returning the price to the caller.
    try:
        # Fire both requests concurrently: quote (needed now) + history for ATR
        # (background, result stored in cache for this and future calls).
        quote_task   = asyncio.create_task(
            client.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=8.0)
        )
        atr_bg_task  = asyncio.create_task(_bg_refresh_atr(client, symbol))

        r = await quote_task
        # atr_bg_task runs in the background; we don't await it here.

        if r.status_code != 200:
            return None
        q = r.json()
        price = q.get("price") or q.get("cmp")
        if not price or float(price) <= 0:
            return None
        vol = q.get("volume")

        # Prefer freshly-refreshed ATR (may already be done by the time we
        # get here if the history call is fast), fall back to whatever was
        # already in cache.
        atr = _cached_atr(symbol)

        return Tick(
            symbol=symbol,
            price=float(price),
            # market-data-service's own fetched_at isn't guaranteed to be a
            # cleanly-parseable tz-aware timestamp across every source branch
            # it can take, so this module stamps its OWN receipt time — which
            # is what risk_engine's staleness check (#7) is actually trying to
            # measure (age since WE last saw a price), not the upstream
            # provider's internal timestamp.
            as_of=datetime.now(timezone.utc),
            atr=atr,
            source=q.get("source") or "market-data-service",
            volume=int(vol) if vol not in (None, "") else None,
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


async def _get_preview(client: httpx.AsyncClient, symbol: str) -> Optional[Tick]:
    """Best-effort last-close / previous-close lookup for a symbol whose LIVE
    tick is missing. This is a NON-TRADEABLE price — it can be yesterday's
    close — used only so the dashboard can show a preview Waiting-at / Stop /
    Target instead of blank '—' columns, and so a WAIT can explain itself. It
    is never used to size or place an order (the ENTER path only runs when a
    real live tick exists). Source is tagged 'preview:last_close' so nothing
    downstream can mistake it for a fresh quote.
    """
    for path in (f"/quote/{symbol}", f"/last-close/{symbol}"):
        try:
            r = await client.get(f"{MARKET_DATA_URL}{path}", timeout=8.0)
            if r.status_code != 200:
                continue
            q = r.json()
            px = (
                q.get("price") or q.get("cmp") or q.get("ltp")
                or q.get("prev_close") or q.get("previous_close")
                or q.get("close") or q.get("last_close")
            )
            if px and float(px) > 0:
                return Tick(
                    symbol=symbol,
                    price=float(px),
                    as_of=datetime.now(timezone.utc),
                    atr=_cached_atr(symbol),   # serve from cache; None is fine for preview
                    source="preview:last_close",
                )
        except Exception as e:
            logger.debug("get_preview(%s) via %s failed: %s", symbol, path, e)
            continue
    return None


async def get_preview_quotes(symbols: list[str]) -> dict[str, Tick]:
    """Bulk best-effort preview (last-close) prices — see _get_preview. Only
    call this for symbols that came back empty from get_quotes()."""
    out: dict[str, Tick] = {}
    if not symbols:
        return out
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_get_preview(client, s) for s in symbols])
    for sym, tick in zip(symbols, results):
        if tick is not None:
            out[sym] = tick
    return out
