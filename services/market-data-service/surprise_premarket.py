"""
Pre-market surprise baselines (≈08:55 IST).

Computes once per session:
  - prev_close
  - avg_15m_volume (daily volume / 25 NSE 15m slots)
  - daily_atr (mean high-low over lookback)
  - high_52w + dist_52w_pct

Writes into Neon table `surprise_static_feed` so intraday scans never
re-query multi-day history on the free-tier dyno.

Sources: yfinance history (free). Optional future: daily_bhavcopy table.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("surprise-premarket")

LOOKBACK_DAYS = int(os.getenv("SURPRISE_LOOKBACK_DAYS", "30"))
MAX_SYMBOLS = int(os.getenv("SURPRISE_MAX_SYMBOLS", "320"))


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


def ensure_schema() -> bool:
    url = _db_url()
    if not url:
        logger.warning("No DATABASE_URL — cannot ensure surprise_static_feed")
        return False
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        ddl = """
        CREATE TABLE IF NOT EXISTS surprise_static_feed (
            symbol VARCHAR(30) PRIMARY KEY,
            prev_close NUMERIC(12, 2) NOT NULL,
            avg_15m_volume BIGINT NOT NULL DEFAULT 10000,
            daily_atr NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
            high_52w NUMERIC(12, 2) NOT NULL,
            dist_52w_pct NUMERIC(8, 2) NOT NULL,
            sector VARCHAR(80),
            is_liquid BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_surprise_static_sym ON surprise_static_feed(symbol);
        """
        with eng.begin() as conn:
            for stmt in ddl.strip().split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(text(s))
        eng.dispose()
        return True
    except Exception as e:
        logger.error("schema ensure failed: %s", e)
        return False


def _yahoo_sym(symbol: str) -> str:
    s = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not s:
        return ""
    # Indices / special cases left to caller
    return f"{s}.NS"


def compute_baseline_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Use yfinance daily bars; return baseline dict or None."""
    try:
        import numpy as np
        import yfinance as yf
    except ImportError as e:
        logger.error("numpy/yfinance required: %s", e)
        return None

    ysym = _yahoo_sym(symbol)
    if not ysym:
        return None
    base = symbol.upper().replace(".NS", "").replace(".BO", "").strip()

    try:
        t = yf.Ticker(ysym)
        # 60d covers ~20–30 trading sessions + buffer for 52w approx via period
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 5:
            return None
        # Last LOOKBACK_DAYS sessions for ATR / avg volume
        tail = hist.tail(max(LOOKBACK_DAYS, 20))
        highs = tail["High"].astype("float64").values
        lows = tail["Low"].astype("float64").values
        closes = tail["Close"].astype("float64").values
        volumes = tail["Volume"].astype("float64").values

        prev_close = float(closes[-1])
        high_52w = float(np.nanmax(hist["High"].astype("float64").values))
        if high_52w <= 0:
            return None
        dist_52w_pct = float(((high_52w - prev_close) / high_52w) * 100.0)
        # ~25 fifteen-minute slots in NSE continuous session
        avg_daily_vol = float(np.nanmean(volumes)) if len(volumes) else 0.0
        avg_15m_vol = int(max(1, avg_daily_vol / 25.0))
        daily_atr = float(np.nanmean(highs - lows)) if len(highs) else 0.0
        is_liquid = avg_daily_vol >= float(os.getenv("SURPRISE_MIN_AVG_VOLUME", "50000"))

        sector = None
        try:
            info = getattr(t, "info", None) or {}
            sector = (info.get("sector") or info.get("industry") or None)
            if sector:
                sector = str(sector)[:80]
        except Exception:
            sector = None

        return {
            "symbol": base,
            "prev_close": round(prev_close, 2),
            "avg_15m_volume": avg_15m_vol,
            "daily_atr": round(daily_atr, 2),
            "high_52w": round(high_52w, 2),
            "dist_52w_pct": round(dist_52w_pct, 2),
            "sector": sector,
            "is_liquid": bool(is_liquid),
        }
    except Exception as e:
        logger.debug("baseline %s failed: %s", base, e)
        return None


def upsert_baselines(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    url = _db_url()
    if not url:
        return 0
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=1)
        sql = text(
            """
            INSERT INTO surprise_static_feed
                (symbol, prev_close, avg_15m_volume, daily_atr, high_52w, dist_52w_pct, sector, is_liquid, updated_at)
            VALUES
                (:symbol, :prev_close, :avg_15m_volume, :daily_atr, :high_52w, :dist_52w_pct, :sector, :is_liquid, NOW())
            ON CONFLICT (symbol) DO UPDATE SET
                prev_close = EXCLUDED.prev_close,
                avg_15m_volume = EXCLUDED.avg_15m_volume,
                daily_atr = EXCLUDED.daily_atr,
                high_52w = EXCLUDED.high_52w,
                dist_52w_pct = EXCLUDED.dist_52w_pct,
                sector = EXCLUDED.sector,
                is_liquid = EXCLUDED.is_liquid,
                updated_at = NOW()
            """
        )
        n = 0
        with eng.begin() as conn:
            for r in rows:
                conn.execute(sql, r)
                n += 1
        eng.dispose()
        return n
    except Exception as e:
        logger.error("upsert_baselines failed: %s", e)
        return 0


def precalculate_surprise_baselines(symbols: List[str]) -> Dict[str, Any]:
    """
    Main entry: schema → compute → upsert.
    Rate-limits yfinance to stay free-tier safe.
    """
    t0 = time.time()
    if not ensure_schema():
        return {"ok": False, "error": "schema_failed", "upserted": 0}

    uniq: List[str] = []
    seen = set()
    for s in symbols:
        b = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    uniq = uniq[:MAX_SYMBOLS]

    interval = float(os.getenv("SURPRISE_YF_INTERVAL_SEC", "0.12"))
    ok_rows: List[Dict[str, Any]] = []
    errors = 0
    for i, sym in enumerate(uniq):
        row = compute_baseline_for_symbol(sym)
        if row:
            ok_rows.append(row)
        else:
            errors += 1
        if interval > 0:
            time.sleep(interval)
        # Batch upsert every 40 symbols to limit memory
        if len(ok_rows) >= 40:
            upsert_baselines(ok_rows)
            ok_rows = []
        if (i + 1) % 50 == 0:
            logger.info("surprise premarket progress %s/%s", i + 1, len(uniq))

    upserted = upsert_baselines(ok_rows) if ok_rows else 0
    # recount approximate
    total_up = upserted + (len(uniq) - errors - len(ok_rows) if False else 0)
    # simpler: final full upsert of remaining already done; report computed
    elapsed = round(time.time() - t0, 1)
    return {
        "ok": True,
        "symbols_requested": len(uniq),
        "computed": len(uniq) - errors,
        "errors": errors,
        "elapsed_sec": elapsed,
        "table": "surprise_static_feed",
    }


def default_universe_from_env() -> List[str]:
    raw = os.getenv("SURPRISE_UNIVERSE", "") or os.getenv("SCAN_UNIVERSE", "")
    if raw.strip():
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    # Small seed; gateway will pass full universe when calling the job
    return [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL",
        "ITC", "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    ]
