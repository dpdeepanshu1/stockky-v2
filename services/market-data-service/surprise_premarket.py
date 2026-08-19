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


# Progress file for UI polling (manual premarket button)
_PROGRESS_PATH = os.getenv(
    "SURPRISE_PREMARKET_PROGRESS_PATH",
    "/tmp/surprise_premarket_progress.json",
)
_job_lock = False


def _write_progress(data: Dict[str, Any]) -> None:
    try:
        import json as _json
        payload = dict(data)
        payload["updated_at"] = time.time()
        with open(_PROGRESS_PATH, "w", encoding="utf-8") as f:
            _json.dump(payload, f)
    except Exception as e:
        logger.debug("progress write: %s", e)


def get_premarket_progress() -> Dict[str, Any]:
    try:
        import json as _json
        with open(_PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "stage": "idle",
        "percent": 0,
        "processed": 0,
        "total": 0,
        "errors": 0,
        "elapsed_sec": 0,
        "eta_sec": None,
        "is_running": False,
        "current_symbol": None,
        "message": "Idle",
    }


def precalculate_surprise_baselines(symbols: List[str]) -> Dict[str, Any]:
    """
    Main entry: schema → compute → upsert.
    Rate-limits yfinance to stay free-tier safe.
    Writes progress JSON for frontend polling.
    """
    global _job_lock
    t0 = time.time()
    if _job_lock:
        return {"ok": False, "error": "already_running", "progress": get_premarket_progress()}
    _job_lock = True

    try:
        if not ensure_schema():
            out = {"ok": False, "error": "schema_failed", "upserted": 0}
            _write_progress({
                "stage": "error",
                "percent": 0,
                "processed": 0,
                "total": 0,
                "errors": 0,
                "elapsed_sec": 0,
                "eta_sec": None,
                "is_running": False,
                "message": "Schema ensure failed (check DATABASE_URL)",
                "error": "schema_failed",
            })
            return out

        uniq: List[str] = []
        seen = set()
        for s in symbols:
            b = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
            if b and b not in seen:
                seen.add(b)
                uniq.append(b)
        uniq = uniq[:MAX_SYMBOLS]
        total = len(uniq)

        _write_progress({
            "stage": "starting",
            "percent": 1,
            "processed": 0,
            "total": total,
            "errors": 0,
            "elapsed_sec": 0,
            "eta_sec": None,
            "is_running": True,
            "current_symbol": None,
            "message": f"Starting baselines for {total} symbols",
        })

        interval = float(os.getenv("SURPRISE_YF_INTERVAL_SEC", "0.12"))
        ok_rows: List[Dict[str, Any]] = []
        errors = 0
        computed = 0
        for i, sym in enumerate(uniq):
            row = compute_baseline_for_symbol(sym)
            if row:
                ok_rows.append(row)
                computed += 1
            else:
                errors += 1
            if interval > 0:
                time.sleep(interval)
            if len(ok_rows) >= 40:
                upsert_baselines(ok_rows)
                ok_rows = []

            processed = i + 1
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0.5 else 0
            remaining = total - processed
            eta = (remaining / rate) if rate > 0 else None
            pct = min(99, int(100 * processed / max(total, 1)))
            _write_progress({
                "stage": "computing",
                "percent": pct,
                "processed": processed,
                "total": total,
                "computed": computed,
                "errors": errors,
                "elapsed_sec": round(elapsed, 1),
                "eta_sec": round(eta, 1) if eta is not None else None,
                "is_running": True,
                "current_symbol": sym,
                "message": f"{processed}/{total} · {sym}",
            })
            if processed % 50 == 0:
                logger.info("surprise premarket progress %s/%s", processed, total)

        upserted = upsert_baselines(ok_rows) if ok_rows else 0
        elapsed = round(time.time() - t0, 1)
        result = {
            "ok": True,
            "symbols_requested": total,
            "computed": computed,
            "errors": errors,
            "elapsed_sec": elapsed,
            "table": "surprise_static_feed",
            "upserted_last_batch": upserted,
        }
        _write_progress({
            "stage": "done",
            "percent": 100,
            "processed": total,
            "total": total,
            "computed": computed,
            "errors": errors,
            "elapsed_sec": elapsed,
            "eta_sec": 0,
            "is_running": False,
            "current_symbol": None,
            "message": f"Done · {computed} baselines · {errors} errors · {elapsed}s",
            "result": result,
        })
        return result
    except Exception as e:
        logger.exception("precalculate_surprise_baselines failed")
        _write_progress({
            "stage": "error",
            "percent": 0,
            "processed": 0,
            "total": 0,
            "errors": 1,
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": None,
            "is_running": False,
            "message": str(e)[:200],
            "error": str(e)[:200],
        })
        return {"ok": False, "error": str(e)[:200]}
    finally:
        _job_lock = False



def default_universe_from_env() -> List[str]:
    raw = os.getenv("SURPRISE_UNIVERSE", "") or os.getenv("SCAN_UNIVERSE", "")
    if raw.strip():
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    # Small seed; gateway will pass full universe when calling the job
    return [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL",
        "ITC", "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    ]
