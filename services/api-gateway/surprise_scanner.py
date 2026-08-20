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
# Universal ≤ ₹5000 gate (root filter also applied in data_feed / bhavcopy)
MAX_STOCK_PRICE = float(os.getenv("MAX_STOCK_PRICE", "5000"))


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
            current_price = float(tick.get("price") or tick.get("last_price") or tick.get("ltp") or tick.get("close") or tick.get("cmp") or 0.0)
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
            px = round(current_price, 2)
            # Universal ≤ ₹5000 gate — never surface high-ticket surprises
            if px > MAX_STOCK_PRICE:
                return None
            hit = {
                "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
                "score": int(score),
                "price": px,
                "change_pct": price_change_pct,
                "rvol": rvol,
                "trigger_type": trigger_type,
                "trailing_stop": round(vwap * 0.985, 2),
                "target_1": round(current_price * 1.05, 2),
                "prev_close": round(prev_close, 2),
                "sector": static.get("sector"),
                "dist_52w_pct": round(dist, 2),
            }
            # Step 6: stamp all FE price aliases (align with price_resolver / priceDisplay)
            hit["cmp"] = px
            hit["ltp"] = px
            hit["last_price"] = px
            hit["close"] = px
            hit["current_price"] = px
            return hit
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


# ── Market-aware live surprise quote cache (anti-429 / 401) ─────────────────
SURPRISE_FEED_CACHE_KEY = "system:surprise_feed"
SURPRISE_FEED_OPEN_TTL_SEC = int(__import__("os").getenv("SURPRISE_FEED_OPEN_TTL_SEC", "7200"))  # 2h
SURPRISE_FEED_COOLDOWN_SEC = float(__import__("os").getenv("SURPRISE_FEED_COOLDOWN_SEC", "0.5"))


def is_market_open_ist() -> bool:
    """NSE continuous session 09:15–15:30 IST, Mon–Fri."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, time as dtime
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() >= 5:
            return False
        try:
            from nse_holidays import is_nse_holiday
            if is_nse_holiday(now.date()):
                return False
        except Exception:
            pass
        tt = now.time()
        return dtime(9, 15) <= tt <= dtime(15, 30)
    except Exception:
        return False


def _read_surprise_feed_cache() -> Optional[dict]:
    try:
        import kv_cache as _kc
        raw = _kc.kv_get(SURPRISE_FEED_CACHE_KEY)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            import json as _json
            return _json.loads(raw)
    except Exception as e:
        logger.debug("surprise feed cache read: %s", e)
    return None


def _write_surprise_feed_cache(payload: dict) -> None:
    try:
        import kv_cache as _kc
        # Durable: long TTL when closed; 2h when open (still stored in Neon)
        ttl = None if not is_market_open_ist() else max(SURPRISE_FEED_OPEN_TTL_SEC, 3600)
        _kc.kv_set(SURPRISE_FEED_CACHE_KEY, payload, ttl=ttl)
    except Exception as e:
        logger.warning("surprise feed cache write: %s", e)


async def run_market_aware_surprise_feed(
    symbols: Optional[list] = None,
    market_data_url: str = "",
    force: bool = False,
) -> dict:
    """
    Live quote batch for surprise dashboard with market-aware Neon cache.

    - Market OPEN: reuse cache if age < 2 hours (default)
    - Market CLOSED (nights/weekends/holidays): reuse cache forever until force=true
    - Live path: sequential quotes with 0.5s cooldown (401 crumb killer)
    """
    import json as _json
    import httpx

    cached = _read_surprise_feed_cache()
    if cached and not force:
        age = time.time() - float(cached.get("timestamp") or 0)
        open_now = is_market_open_ist()
        if not open_now:
            return {
                "status": "success",
                "source": "cache",
                "market_open": False,
                "age_sec": int(age),
                "data": cached.get("data") or [],
                "message": "Market closed — serving durable cache (no API calls)",
            }
        if age < SURPRISE_FEED_OPEN_TTL_SEC:
            return {
                "status": "success",
                "source": "cache",
                "market_open": True,
                "age_sec": int(age),
                "data": cached.get("data") or [],
                "message": f"Cache hit ({int(age)}s old, TTL {SURPRISE_FEED_OPEN_TTL_SEC}s)",
            }

    # Resolve symbol list
    syms = []
    if symbols:
        syms = [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in symbols if s]
    if not syms:
        # Prefer static cache keys, else empty
        if surprise_engine.static_cache:
            syms = list(surprise_engine.static_cache.keys())
        else:
            try:
                surprise_engine.load_static_cache()
                syms = list(surprise_engine.static_cache.keys())
            except Exception:
                syms = []
    # de-dupe
    seen = set()
    clean = []
    for s in syms:
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    syms = clean

    if not syms:
        return {
            "status": "error",
            "source": "live",
            "data": [],
            "message": "No symbols — run premarket baselines first",
        }

    # CHUNKED YF DOWNLOAD (50-ticker chunks, period=2d, threads=True) — primary path
    results = []
    errors = 0
    got = set()
    chunk_size = 50

    def _clean_sym(s: str) -> str:
        u = str(s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        u = u.replace("%20", " ")
        # Light local map (gateway may not import market-data sanitize)
        _map = {
            "KFIN TECHNOLOGIES": "KFINTECH",
            "KPIT TECHNOLOGIES": "KPITTECH",
            "360 ONE": "360ONE",
            "360ONE WAM": "360ONE",
            "PB FINTECH": "POLICYBZR",
            "HONASA CONSUMER": "HONASA",
        }
        if u in _map:
            return _map[u]
        u = u.replace(" TECHNOLOGIES", "TECH").replace(" TECHNOLOGY", "TECH")
        u = u.replace(" LIMITED", "").replace(" LTD", "").replace(" ", "")
        return u

    try:
        import yfinance as yf
        clean_syms = []
        seen_c = set()
        for s in syms:
            c = _clean_sym(s)
            if c and c not in seen_c:
                seen_c.add(c)
                clean_syms.append(c)
        syms = clean_syms or syms

        for i in range(0, len(syms), chunk_size):
            chunk = syms[i : i + chunk_size]
            ticker_string = " ".join(f"{s}.NS" for s in chunk)
            try:
                df = yf.download(
                    ticker_string,
                    period="2d",
                    group_by="ticker",
                    threads=True,
                    progress=False,
                    auto_adjust=True,
                )
                for s in chunk:
                    try:
                        sym_ns = f"{s}.NS"
                        sub = None
                        if df is not None and hasattr(df, "columns") and getattr(df.columns, "nlevels", 1) > 1:
                            if sym_ns in df.columns.get_level_values(0):
                                sub = df[sym_ns]
                        elif len(chunk) == 1:
                            sub = df
                        if sub is None or (hasattr(sub, "empty") and sub.empty):
                            continue
                        if "Close" not in getattr(sub, "columns", []):
                            continue
                        series = sub["Close"].dropna()
                        if series.empty:
                            continue
                        px = float(series.iloc[-1])
                        if px <= 0 or px > 5000:
                            continue
                        prev = px
                        if len(series) >= 2:
                            prev = float(series.iloc[-2])
                        chg = round(((px - prev) / prev) * 100, 2) if prev > 0 else None
                        high = low = vol = None
                        try:
                            if "High" in sub.columns:
                                high = float(sub["High"].dropna().iloc[-1])
                            if "Low" in sub.columns:
                                low = float(sub["Low"].dropna().iloc[-1])
                            if "Volume" in sub.columns:
                                vol = int(float(sub["Volume"].dropna().iloc[-1]))
                                if vol < 0:
                                    vol = None
                        except Exception:
                            pass
                        row = {
                            "symbol": s,
                            "price": px,
                            "cmp": px,
                            "previous_close": prev,
                            "day_change_pct": chg,
                            "day_high": high,
                            "day_low": low,
                            "source": "yahoo_bulk",
                        }
                        if vol is not None:
                            row["volume"] = vol
                        row = {k: v for k, v in row.items() if v is not None}
                        results.append(row)
                        got.add(s)
                    except Exception:
                        errors += 1
            except Exception as e:
                logger.warning("surprise yf chunk %s: %s", i, e)
                errors += 1
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("surprise chunked yf unavailable: %s", e)

    # Waterfall fill for misses via market-data /quote
    misses = [s for s in syms if s not in got]
    md = (market_data_url or "").rstrip("/")
    if misses and md:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            for sym in misses:
                try:
                    r = await client.get(f"{md}/quote/{sym}")
                    if r.status_code == 200:
                        body = r.json() if isinstance(r.json(), dict) else {}
                        px = None
                        for k in ("price", "cmp", "ltp", "close", "last_price", "regularMarketPrice"):
                            try:
                                v = float(body.get(k) or 0)
                                if v > 0:
                                    px = v
                                    break
                            except (TypeError, ValueError):
                                pass
                        if px is not None and 0 < px <= 5000:
                            results.append({"symbol": sym, "price": px, "cmp": px, "source": body.get("source") or "waterfall"})
                            got.add(sym)
                    elif r.status_code in (401, 429):
                        errors += 1
                        await asyncio.sleep(SURPRISE_FEED_COOLDOWN_SEC * 2)
                except Exception:
                    errors += 1
                await asyncio.sleep(SURPRISE_FEED_COOLDOWN_SEC)

    payload = {
        "timestamp": time.time(),
        "data": results,
        "count": len(results),
        "errors": errors,
        "market_open": is_market_open_ist(),
    }
    _write_surprise_feed_cache(payload)
    return {
        "status": "success",
        "source": "live",
        "market_open": is_market_open_ist(),
        "data": results,
        "count": len(results),
        "errors": errors,
        "message": f"Live surprise feed: {len(results)} quotes, {errors} errors",
    }


def audit_surprise_feed() -> dict:
    """Health snapshot for the Surprise dashboard (mirrors data-feed audit)."""
    cached = _read_surprise_feed_cache() or {}
    data = cached.get("data") if isinstance(cached, dict) else []
    if not isinstance(data, list):
        data = []
    total = len(data)
    missing = []
    complete = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        px = 0.0
        for k in ("price", "cmp", "ltp", "close"):
            try:
                v = float(row.get(k) or 0)
                if v > 0:
                    px = v
                    break
            except (TypeError, ValueError):
                pass
        if px <= 0:
            missing.append({"symbol": sym, "missing_fields": ["price"]})
        else:
            complete += 1
    health = round((complete / max(total, 1)) * 100, 1) if total > 0 else 0.0
    return {
        "ok": True,
        "total_tracked": total,
        "fully_populated": complete,
        "missing_data": len(missing),
        "health_score": health,
        "incomplete_stocks": missing[:200],
        "cache_age_sec": int(time.time() - float(cached.get("timestamp") or 0)) if cached else None,
        "market_open": is_market_open_ist(),
        "source": "cache" if cached else "empty",
    }


def repair_surprise_batch(
    limit: int = 15,
    market_data_url: str = "",
    symbol: str = None,
) -> dict:
    """
    Waterfall fill for missing surprise quote rows only.
    Hits market-data /quote (Yahoo → TwelveData → Polygon) with 0.5s cooldown.
    If symbol= is set, only that ticker is repaired (single-row UI button).
    """
    import os
    import httpx

    cached = _read_surprise_feed_cache() or {}
    data_list = list(cached.get("data") or [])
    if not data_list:
        return {"status": "no_data", "repaired": []}

    force_sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip() or None

    targets = []
    if force_sym:
        # Ensure the symbol exists in the cache list (or append a stub to patch)
        found = False
        for item in data_list:
            if str(item.get("symbol") or "").upper() == force_sym:
                found = True
                break
        if not found:
            data_list.append({"symbol": force_sym, "price": 0, "cmp": 0})
        targets = [force_sym]
    else:
        for item in data_list:
            if not isinstance(item, dict):
                continue
            try:
                px = float(item.get("price") or item.get("cmp") or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                sym = str(item.get("symbol") or "").upper().strip()
                if sym:
                    targets.append(sym)
        targets = targets[: max(1, min(int(limit or 15), 30))]
    if not targets:
        return {"status": "completed", "repaired": [], "message": "Nothing missing"}

    md = (market_data_url or os.getenv("MARKET_DATA_URL") or "").rstrip("/")
    repaired = []
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            for sym in targets:
                try:
                    r = client.get(f"{md}/quote/{sym}")
                    if r.status_code == 200:
                        body = r.json() if isinstance(r.json(), dict) else {}
                        px = None
                        for k in ("price", "cmp", "ltp", "close", "last_price"):
                            try:
                                v = float(body.get(k) or 0)
                                if v > 0:
                                    px = v
                                    break
                            except (TypeError, ValueError):
                                pass
                        if px is None or px > 5000:
                            time.sleep(SURPRISE_FEED_COOLDOWN_SEC)
                            continue
                        for item in data_list:
                            if str(item.get("symbol") or "").upper() == sym:
                                item["price"] = px
                                item["cmp"] = px
                                item["source"] = body.get("source") or "waterfall"
                                repaired.append(sym)
                                break
                except Exception:
                    pass
                time.sleep(SURPRISE_FEED_COOLDOWN_SEC)
    except Exception as e:
        logger.warning("repair_surprise_batch: %s", e)

    cached["data"] = data_list
    cached["timestamp"] = time.time()
    _write_surprise_feed_cache(cached)
    return {
        "status": "completed",
        "repaired": repaired,
        "repaired_count": len(repaired),
        "targets": targets,
    }
