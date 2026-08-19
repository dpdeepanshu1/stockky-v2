"""
Lightweight intraday Surprise Stock engine.

Design (free-tier / 512MB safe):
  1. Load all baselines from Neon `surprise_static_feed` in ONE query.
  2. Fetch live ticks in small concurrent batches (Semaphore).
  3. Score in pure Python (no heavy history pulls).

Score formula (0–100):
  - Volume burst (RVOL) ……… up to 35
  - ORB / VWAP breakout ……… up to 30
  - Proximity to 52W high … up to 20
  - Range vs daily ATR ……… up to 15
Surface only score >= 60 and change_pct > 1%.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("surprise-scanner")

MIN_SCORE = int(os.getenv("SURPRISE_MIN_SCORE", "60"))
MIN_CHANGE_PCT = float(os.getenv("SURPRISE_MIN_CHANGE_PCT", "1.0"))
CONCURRENCY = int(os.getenv("SURPRISE_SCAN_CONCURRENCY", "20"))
QUOTE_TIMEOUT = float(os.getenv("SURPRISE_QUOTE_TIMEOUT", "3"))


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _db_url() -> Optional[str]:
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    return _normalize_db_url(url) if url else None


class SurpriseStockEngine:
    def __init__(self):
        self.static_cache: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self.semaphore = asyncio.Semaphore(max(4, min(CONCURRENCY, 20)))

    def load_static_cache(self, force: bool = False) -> int:
        """Sync load of all baselines — one SQL round-trip. Creates table if missing."""
        if self.static_cache and not force and (time.time() - self._loaded_at) < 300:
            return len(self.static_cache)
        url = _db_url()
        if not url:
            logger.warning("surprise: no DATABASE_URL — static cache empty")
            self.static_cache = {}
            return 0
        try:
            # Always ensure table exists before SELECT (fixes UndefinedTable on first deploy)
            try:
                from surprise_schema import ensure_surprise_schema

                ensure_surprise_schema()
            except Exception as se:
                logger.debug("ensure_surprise_schema: %s", se)

            from sqlalchemy import create_engine, text

            eng = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=1,
                pool_timeout=8,
                connect_args={"connect_timeout": 8, "application_name": "stockky-surprise-scan"},
            )
            with eng.begin() as conn:
                # Idempotent DDL in case ensure_schema module unavailable
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS surprise_static_feed (
                            symbol VARCHAR(30) PRIMARY KEY,
                            prev_close NUMERIC(12, 2) NOT NULL DEFAULT 0,
                            avg_15m_volume BIGINT NOT NULL DEFAULT 10000,
                            daily_atr NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
                            high_52w NUMERIC(12, 2) NOT NULL DEFAULT 0,
                            dist_52w_pct NUMERIC(8, 2) NOT NULL DEFAULT 100,
                            sector VARCHAR(80),
                            is_liquid BOOLEAN DEFAULT TRUE,
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                        """
                    )
                )
                # Sticky Fix Step 4: explicit columns (match surprise_premarket INSERT)
                rows = conn.execute(
                    text(
                        "SELECT symbol, prev_close, avg_15m_volume, daily_atr, "
                        "high_52w, dist_52w_pct, sector, is_liquid, updated_at "
                        "FROM surprise_static_feed"
                    )
                ).mappings().all()
            eng.dispose()
            cache: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                d = dict(r)
                sym = str(d.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                for k in ("prev_close", "daily_atr", "high_52w", "dist_52w_pct"):
                    try:
                        d[k] = float(d[k]) if d.get(k) is not None else 0.0
                    except Exception:
                        d[k] = 0.0
                # avg_15m_volume OR legacy avg_volume
                try:
                    vol = d.get("avg_15m_volume")
                    if vol is None:
                        vol = d.get("avg_volume")
                    d["avg_15m_volume"] = int(vol or 10000)
                except Exception:
                    d["avg_15m_volume"] = 10000
                cache[sym] = d
            self.static_cache = cache
            self._loaded_at = time.time()
            logger.info("surprise static cache loaded: %s symbols", len(cache))
            return len(cache)
        except Exception as e:
            logger.warning("load_static_cache failed: %s", e)
            return len(self.static_cache)


    def score_stock(self, symbol: str, tick: dict) -> Optional[dict]:
        static = self.static_cache.get(symbol.upper().replace(".NS", "").replace(".BO", ""))
        if not static:
            return None

        try:
            current_price = float(tick.get("price") or tick.get("close") or tick.get("ltp") or 0.0)
        except Exception:
            current_price = 0.0
        prev_close = float(static.get("prev_close") or 0.0)
        # If live quote missing/rate-limited, still finish using baseline prev_close
        if current_price <= 0 and prev_close > 0:
            current_price = prev_close
            tick = dict(tick or {})
            tick["_price_from_baseline"] = True
        if current_price <= 0:
            return None
        if prev_close <= 0:
            prev_close = current_price
        open_price = float(tick.get("open") or prev_close)
        current_vol_15m = float(tick.get("vol_15m") or tick.get("volume") or 0.0)
        # If only full-day volume is available, approximate current 15m slice
        if current_vol_15m > 0 and tick.get("vol_15m") is None:
            # crude: assume volume accumulates over ~25 slots; scale by session progress if provided
            progress = float(tick.get("session_progress") or 0.4)
            progress = max(0.15, min(progress, 1.0))
            current_vol_15m = current_vol_15m / max(1.0, 25.0 * progress)

        orb_high = float(tick.get("orb_high") or tick.get("high") or open_price)
        vwap = float(tick.get("vwap") or current_price)
        day_high = float(tick.get("high") or current_price)
        day_low = float(tick.get("low") or current_price)

        avg_15m_vol = max(int(static.get("avg_15m_volume") or 1), 1)
        rvol = round(current_vol_15m / avg_15m_vol, 2) if avg_15m_vol else 0.0
        price_change_pct = round(((current_price - prev_close) / prev_close) * 100.0, 2) if prev_close else 0.0

        score = 0
        trigger_type = "Consolidation"

        # 1. Volume Burst (35)
        if rvol >= 3.5:
            score += 35
        elif rvol >= 2.0:
            score += 20
        elif rvol >= 1.5:
            score += 10

        # 2. ORB / VWAP (30)
        if current_price > orb_high and current_price > vwap:
            score += 30
            trigger_type = "Morning ORB Breakout"
        elif current_price > vwap:
            score += 15
            if trigger_type == "Consolidation":
                trigger_type = "Above VWAP"

        # 3. 52W proximity (20)
        dist = float(static.get("dist_52w_pct") or 100.0)
        if dist <= 5.0:
            score += 20
            if score >= 50:
                trigger_type = "Near 52W High"
        elif dist <= 12.0:
            score += 10

        # 4. Range expansion (15)
        daily_atr = float(static.get("daily_atr") or 0.0)
        intraday_range = max(0.0, day_high - day_low)
        if daily_atr > 0 and intraday_range >= (daily_atr * 0.8):
            score += 15
            if trigger_type == "Consolidation":
                trigger_type = "Range Expansion"

        if score >= MIN_SCORE and price_change_pct > MIN_CHANGE_PCT:
            return {
                "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
                "score": int(score),
                "price": round(current_price, 2),
                "change_pct": price_change_pct,
                "rvol": rvol,
                "trigger_type": trigger_type,
                "trailing_stop": round(vwap * 0.985, 2),
                "target_1": round(current_price * 1.05, 2),
                "prev_close": round(prev_close, 2),
                "sector": static.get("sector"),
                "dist_52w_pct": round(dist, 2),
            }
        return None

    async def _fetch_quote(
        self,
        client: httpx.AsyncClient,
        market_data_url: str,
        symbol: str,
    ) -> Optional[dict]:
        async with self.semaphore:
            try:
                r = await client.get(
                    f"{market_data_url.rstrip('/')}/quote/{symbol}",
                    timeout=QUOTE_TIMEOUT,
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                if not isinstance(data, dict):
                    return None
                # Normalize field names from market-data QuoteResponse
                price = data.get("price") or data.get("close") or data.get("regularMarketPrice")
                return {
                    "price": price,
                    "close": data.get("close") or price,
                    "open": data.get("open") or data.get("regularMarketOpen"),
                    "high": data.get("high") or data.get("dayHigh") or data.get("regularMarketDayHigh"),
                    "low": data.get("low") or data.get("dayLow") or data.get("regularMarketDayLow"),
                    "volume": data.get("volume") or data.get("regularMarketVolume"),
                    "vwap": data.get("vwap"),
                    "orb_high": data.get("orb_high"),
                    "vol_15m": data.get("vol_15m"),
                }
            except Exception as e:
                logger.debug("quote %s: %s", symbol, e)
                return None

    async def scan(
        self,
        client: httpx.AsyncClient,
        market_data_url: str,
        symbols: Optional[List[str]] = None,
        force_reload_static: bool = False,
    ) -> Dict[str, Any]:
        t0 = time.time()
        n_static = self.load_static_cache(force=force_reload_static)
        if n_static == 0:
            return {
                "count": 0,
                "stocks": [],
                "static_loaded": 0,
                "error": "surprise_static_feed empty — run premarket job first",
                "elapsed_sec": round(time.time() - t0, 2),
            }

        if symbols:
            keys = [
                s.upper().replace(".NS", "").replace(".BO", "").strip()
                for s in symbols
                if s
            ]
            keys = [k for k in keys if k in self.static_cache]
        else:
            # Prefer liquid names when flag present
            keys = [
                k
                for k, v in self.static_cache.items()
                if v.get("is_liquid", True)
            ]
            if not keys:
                keys = list(self.static_cache.keys())

        results: List[dict] = []
        quote_ok = 0
        chunk = 25
        for i in range(0, len(keys), chunk):
            batch = keys[i : i + chunk]
            ticks = await asyncio.gather(
                *[self._fetch_quote(client, market_data_url, sym) for sym in batch],
                return_exceptions=True,
            )
            for sym, tick in zip(batch, ticks):
                if isinstance(tick, Exception) or not tick:
                    # Score with empty tick → uses prev_close baseline so scan finishes
                    scored = self.score_stock(sym, {})
                else:
                    quote_ok += 1
                    scored = self.score_stock(sym, tick)
                if scored:
                    results.append(scored)

        results.sort(key=lambda x: x["score"], reverse=True)
        return {
            "count": len(results),
            "stocks": results,
            "static_loaded": n_static,
            "quotes_ok": quote_ok,
            "universe_scanned": len(keys),
            "elapsed_sec": round(time.time() - t0, 2),
            "min_score": MIN_SCORE,
        }


surprise_engine = SurpriseStockEngine()
