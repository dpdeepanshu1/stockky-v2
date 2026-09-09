"""
scheduler/fundamentals_batch.py  §7 — nightly fundamentals batch worker.

Iterates the full scan universe (via API gateway), fetches fundamentals for
each symbol (IndianAPI primary, yfinance fallback — same as the existing
/fundamentals/{symbol} endpoint), writes into fundamentals_cache table.

Live scans read the table only — zero yfinance calls in the critical path.
Rate-paced through the existing rate_limiter pattern (~2 req/s → ~400 symbols
in ~200s). Run nightly pre-market, off-hours, to avoid competing with live
quote traffic.

Same shape as weekend_hydrator.py — reuses _fetch_universe() pattern.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("fundamentals-batch")

FUNDAMENTAL_URL = os.getenv(
    "FUNDAMENTAL_URL",
    "https://analysis-intelligence-service.onrender.com/fundamental",
).rstrip("/")
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "https://api-gateway.onrender.com").rstrip("/")
DB_URL = (
    os.getenv("CACHE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
)

BATCH_DELAY_S    = float(os.getenv("FUNDAMENTALS_BATCH_DELAY_S", "0.5"))   # ~2 req/s
REQUEST_TIMEOUT  = float(os.getenv("FUNDAMENTALS_REQUEST_TIMEOUT", "30.0"))
MAX_SYMBOLS      = int(os.getenv("FUNDAMENTALS_MAX_SYMBOLS", "500"))


def _fetch_universe() -> list:
    for url in [
        f"{API_GATEWAY_URL}/scan/universe",
        f"{API_GATEWAY_URL}/api/universe",
        f"{API_GATEWAY_URL}/universe",
    ]:
        try:
            r = httpx.get(url, timeout=60.0)
            if r.status_code == 200:
                data = r.json()
                syms = data if isinstance(data, list) else (
                    data.get("symbols") or data.get("universe") or []
                )
                out = [str(s).upper().strip() for s in syms if s]
                if out:
                    logger.info("Universe from %s: %d symbols", url, len(out))
                    return out
        except Exception as e:
            logger.debug("Universe fetch %s: %s", url, e)
    logger.warning("Could not fetch universe — fundamentals batch will use empty list")
    return []


def _ensure_table(conn) -> None:
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fundamentals_cache (
            symbol      TEXT        PRIMARY KEY,
            data_json   JSONB,
            updated_at  TIMESTAMPTZ DEFAULT now()
        )
    """))


def _fetch_fundamentals(client: httpx.Client, symbol: str) -> Optional[dict]:
    try:
        r = client.get(
            f"{FUNDAMENTAL_URL}/analyze/{symbol}",
            params={"force": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("fundamentals fetch %s: %s", symbol, e)
    return None


def run_batch() -> dict:
    """Run the full fundamentals batch. Returns summary dict."""
    symbols = _fetch_universe()
    if not symbols:
        return {"error": "empty universe", "written": 0}

    symbols = symbols[:MAX_SYMBOLS]
    if not DB_URL:
        logger.error("No DATABASE_URL — cannot write fundamentals_cache.")
        return {"error": "no db url", "written": 0}

    url = DB_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    from sqlalchemy import create_engine, text
    import json as _json

    engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    with engine.begin() as conn:
        _ensure_table(conn)

    written = errors = 0
    now = datetime.now(timezone.utc)

    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for sym in symbols:
            data = _fetch_fundamentals(client, sym)
            if data:
                try:
                    with engine.begin() as conn:
                        conn.execute(text("""
                            INSERT INTO fundamentals_cache (symbol, data_json, updated_at)
                            VALUES (:sym, :data, :now)
                            ON CONFLICT (symbol) DO UPDATE
                              SET data_json=EXCLUDED.data_json,
                                  updated_at=EXCLUDED.updated_at
                        """), {
                            "sym":  sym,
                            "data": _json.dumps(data),
                            "now":  now,
                        })
                    written += 1
                except Exception as e:
                    logger.warning("DB write failed for %s: %s", sym, e)
                    errors += 1
            else:
                errors += 1
            time.sleep(BATCH_DELAY_S)

    logger.info(
        "fundamentals_batch done: written=%d errors=%d total=%d",
        written, errors, len(symbols),
    )
    return {"written": written, "errors": errors, "total": len(symbols), "ts": now.isoformat()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_batch()
    print(result)
