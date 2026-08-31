"""
scheduler/symbol_master_sync.py  §4 — nightly symbol_master sync.

Pulls Nifty 500 constituent list + 11-12 sectoral index constituent lists
from NSE, upserts into symbol_master. Marks delisted/suspended symbols.
Run nightly (pre-market). Handles routine churn: new listings, delistings,
suspensions happen continuously, not as rare edge cases.

SECTOR TAXONOMY (per PDF §4):
  sector  = coarse (11-12 official sectoral indices) — primary bucket for
             relative-strength comparisons (bigger sample per bucket).
  industry = fine (72 Nifty 500 industries) — tie-break ranking only.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("symbol-master-sync")

DB_URL = (
    os.getenv("CACHE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or os.getenv("TRAINING_DATABASE_URL")
    or ""
)

# NSE official sectoral index names → coarse sector label
_SECTORAL_INDICES = {
    "Nifty Bank":              "Bank",
    "Nifty IT":                "IT",
    "Nifty Auto":              "Auto",
    "Nifty Pharma":            "Pharma",
    "Nifty FMCG":              "FMCG",
    "Nifty Metal":             "Metal",
    "Nifty Realty":            "Realty",
    "Nifty Media":             "Media",
    "Nifty PSU Bank":          "PSU Bank",
    "Nifty Private Bank":      "Private Bank",
    "Nifty Financial Services": "Financial Services",
    "Nifty Energy":            "Energy",
}

_NSE_BASE = "https://www.nseindia.com"
_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://www.nseindia.com/",
}


def _nse_session() -> httpx.Client:
    c = httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=20.0)
    try:
        c.get(_NSE_BASE, timeout=10.0)  # seed cookies
    except Exception:
        pass
    return c


def _fetch_index_constituents(client: httpx.Client, index_name: str) -> list[dict]:
    """Fetch constituent list for one NSE index. Returns list of {symbol, isin} dicts."""
    try:
        r = client.get(
            f"{_NSE_BASE}/api/equity-stockIndices",
            params={"index": index_name},
        )
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
        return [
            {"symbol": item["symbol"].upper(), "isin": item.get("meta", {}).get("isin") or ""}
            for item in data
            if isinstance(item, dict) and item.get("symbol")
        ]
    except Exception as e:
        logger.warning("_fetch_index_constituents(%s): %s", index_name, e)
        return []


def _fetch_nifty500_industries(client: httpx.Client) -> dict[str, str]:
    """Returns {symbol: industry_name} from Nifty 500 file."""
    try:
        r = client.get(
            f"{_NSE_BASE}/api/equity-stockIndices",
            params={"index": "Nifty 500"},
        )
        if r.status_code != 200:
            return {}
        data = r.json().get("data") or []
        result = {}
        for item in data:
            if isinstance(item, dict) and item.get("symbol"):
                sym = item["symbol"].upper()
                industry = item.get("industry") or item.get("sector") or ""
                result[sym] = industry
        return result
    except Exception as e:
        logger.warning("_fetch_nifty500_industries: %s", e)
        return {}


def run_sync() -> dict:
    """Run the full symbol_master sync. Returns summary dict."""
    if not DB_URL:
        logger.error("No DATABASE_URL — symbol_master sync skipped.")
        return {"error": "no db url", "upserted": 0}

    from sqlalchemy import create_engine, text

    url = DB_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)

    # Ensure table exists
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS symbol_master (
                current_symbol   TEXT        PRIMARY KEY,
                isin             TEXT        UNIQUE,
                prior_symbols    TEXT[],
                sector           TEXT,
                industry         TEXT,
                status           TEXT        DEFAULT 'active',
                merged_into      TEXT,
                last_verified_at TIMESTAMPTZ DEFAULT now()
            )
        """))

    client = _nse_session()
    now = datetime.now(timezone.utc)

    # Build symbol → sector map from sectoral indices
    symbol_sector: dict[str, str] = {}
    symbol_isin:   dict[str, str] = {}
    for index_name, sector_label in _SECTORAL_INDICES.items():
        time.sleep(1.0)  # respect NSE rate limits
        constituents = _fetch_index_constituents(client, index_name)
        for item in constituents:
            sym = item["symbol"]
            symbol_sector.setdefault(sym, sector_label)  # first sector wins
            if item["isin"]:
                symbol_isin[sym] = item["isin"]

    # Add industry from Nifty 500
    time.sleep(1.0)
    symbol_industry = _fetch_nifty500_industries(client)

    # Also get ALL NSE securities for status tracking
    time.sleep(1.0)
    try:
        all_r = client.get(
            f"{_NSE_BASE}/api/equity-stockIndices",
            params={"index": "SECURITIES IN NSE"},
        )
        all_syms = set()
        if all_r.status_code == 200:
            for item in (all_r.json().get("data") or []):
                if isinstance(item, dict) and item.get("symbol"):
                    all_syms.add(item["symbol"].upper())
    except Exception:
        all_syms = set(symbol_sector.keys())

    upserted = 0
    with engine.begin() as conn:
        # Upsert active symbols
        for sym in all_syms:
            conn.execute(text("""
                INSERT INTO symbol_master
                  (current_symbol, isin, sector, industry, status, last_verified_at)
                VALUES (:sym, :isin, :sector, :industry, 'active', :now)
                ON CONFLICT (current_symbol) DO UPDATE
                  SET isin=COALESCE(EXCLUDED.isin, symbol_master.isin),
                      sector=COALESCE(EXCLUDED.sector, symbol_master.sector),
                      industry=COALESCE(EXCLUDED.industry, symbol_master.industry),
                      status='active',
                      last_verified_at=EXCLUDED.last_verified_at
            """), {
                "sym":      sym,
                "isin":     symbol_isin.get(sym, ""),
                "sector":   symbol_sector.get(sym, ""),
                "industry": symbol_industry.get(sym, ""),
                "now":      now,
            })
            upserted += 1

        # Mark symbols not in today's NSE list as potentially delisted
        if all_syms:
            conn.execute(text("""
                UPDATE symbol_master
                SET status = 'delisted', last_verified_at = :now
                WHERE current_symbol NOT IN :syms
                  AND status = 'active'
                  AND last_verified_at < :cutoff
            """), {
                "syms":   tuple(all_syms) if len(all_syms) > 1 else (list(all_syms)[0], list(all_syms)[0]),
                "now":    now,
                "cutoff": now,
            })

    client.close()
    logger.info("symbol_master sync complete: %d upserted", upserted)
    return {"upserted": upserted, "sectors_mapped": len(symbol_sector), "ts": now.isoformat()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_sync()
    print(result)
