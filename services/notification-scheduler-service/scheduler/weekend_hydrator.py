"""
Weekend / off-hours fundamental hydrator.

Time-sliced across a 48-hour window so free-tier cron can warm the full
universe without slamming rate limits in one shot.

Designed to be invoked hourly by GitHub Actions (or any cron) over the weekend.
Each run processes ~1/48 of the scan universe with force=true so caches are
refreshed for Monday open.

Usage:
  python -m weekend_hydrator
  # or: python weekend_hydrator.py
"""
from __future__ import annotations

import logging
import math
import os
import time

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weekend-hydrator")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://localhost:8000").rstrip("/")
ANALYSIS_URL = os.getenv(
    "ANALYSIS_INTELLIGENCE_URL",
    os.getenv("FUNDAMENTAL_URL", "http://localhost:8002"),
).rstrip("/")
# Prefer explicit fundamental path if ANALYSIS is the root host
if not ANALYSIS_URL.endswith("/fundamental"):
    # If caller set ANALYSIS_INTELLIGENCE_URL to the root, append /fundamental
    _fund = os.getenv("FUNDAMENTAL_URL", "").rstrip("/")
    if _fund:
        ANALYSIS_URL = _fund
    else:
        ANALYSIS_URL = f"{ANALYSIS_URL}/fundamental"

DELAY_SEC = float(os.getenv("HYDRATOR_DELAY_SEC", "12.0"))
BATCH_HOURS = int(os.getenv("HYDRATOR_BATCH_HOURS", "48"))  # slice universe into N hourly chunks
REQUEST_TIMEOUT = float(os.getenv("HYDRATOR_TIMEOUT_SEC", "45.0"))


def _fetch_universe() -> list[str]:
    """Pull current scan universe from the API gateway."""
    urls = [
        f"{API_GATEWAY_URL}/scan/universe",
        f"{API_GATEWAY_URL}/api/universe",
        f"{API_GATEWAY_URL}/universe",
    ]
    for url in urls:
        try:
            resp = httpx.get(url, timeout=30.0)
            if resp.status_code != 200:
                continue
            data = resp.json() if resp.content else {}
            # Support several response shapes
            if isinstance(data, list):
                symbols = data
            elif isinstance(data, dict):
                symbols = (
                    data.get("symbols")
                    or data.get("universe")
                    or data.get("tickers")
                    or []
                )
            else:
                symbols = []
            out = sorted({str(s).strip().upper().replace(".NS", "").replace(".BO", "") for s in symbols if s})
            if out:
                logger.info("Universe from %s: %d symbols", url, len(out))
                return out
        except Exception as e:
            logger.debug("Universe fetch %s failed: %s", url, e)
    logger.warning("No universe available — empty batch")
    return []


def hydrate_batch(hour_idx: int | None = None) -> dict:
    """
    Hydrate one time-slice of the universe with force=true fundamental analysis.

    hour_idx: 0..BATCH_HOURS-1. Defaults to current UTC hour modulo BATCH_HOURS
    so hourly cron automatically walks the full universe over a weekend.
    """
    symbols = _fetch_universe()
    if not symbols:
        return {"ok": False, "error": "empty_universe", "processed": 0, "total": 0}

    if hour_idx is None:
        hour_idx = int(time.time() / 3600) % BATCH_HOURS
    hour_idx = max(0, min(int(hour_idx), BATCH_HOURS - 1))

    chunk_size = max(1, math.ceil(len(symbols) / BATCH_HOURS))
    start = hour_idx * chunk_size
    end = min(start + chunk_size, len(symbols))
    batch = symbols[start:end]

    logger.info(
        "Hydrating slice %d/%d: symbols[%d:%d] = %d names (delay=%.1fs)",
        hour_idx + 1,
        BATCH_HOURS,
        start,
        end,
        len(batch),
        DELAY_SEC,
    )

    ok_count = 0
    err_count = 0
    rate_limited = False

    def _persist_fundamentals(client: httpx.Client, sym: str, analysis: dict) -> None:
        """Selectively merge real fundamental fields into Neon via gateway (merge, never wipe)."""
        if not isinstance(analysis, dict):
            return
        # Pull common fundamental keys from analysis payload / nested metrics
        metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
        row = {"symbol": sym, "source": "weekend_hydrator"}
        for key in (
            "pe_ratio", "roe", "roce", "debt_to_equity", "revenue_growth",
            "market_cap", "sector", "industry", "quality_score", "fundamental_score",
            "promoter_holding", "eps", "book_value", "dividend_yield",
        ):
            val = analysis.get(key)
            if val is None:
                val = metrics.get(key)
            if val is not None:
                row[key] = val
                # Clear seed flags so merge treats these as real values
                seed_k = f"{key}_seed"
                row[seed_k] = False
        if analysis.get("summary"):
            row["fundamental_summary"] = analysis.get("summary")
        if len(row) <= 2:
            return  # nothing useful beyond symbol/source
        try:
            pr = client.post(f"{API_GATEWAY_URL}/data-feed/update", json=row, timeout=20.0)
            if pr.status_code >= 400:
                logger.debug("persist %s → HTTP %s", sym, pr.status_code)
        except Exception as e:
            logger.debug("persist %s failed: %s", sym, e)

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for i, sym in enumerate(batch):
            try:
                r = client.get(f"{ANALYSIS_URL}/analyze/{sym}?force=true")
                if r.status_code == 200:
                    ok_count += 1
                    try:
                        body = r.json() if r.content else {}
                        _persist_fundamentals(client, sym, body)
                    except Exception:
                        pass
                    logger.info("[%d/%d] %s OK", i + 1, len(batch), sym)
                elif r.status_code == 429:
                    rate_limited = True
                    err_count += 1
                    logger.warning("[%d/%d] %s rate-limited (429) — stopping batch", i + 1, len(batch), sym)
                    break
                else:
                    err_count += 1
                    logger.warning("[%d/%d] %s HTTP %s", i + 1, len(batch), sym, r.status_code)
            except Exception as e:
                err_count += 1
                logger.error("[%d/%d] Error hydrating %s: %s", i + 1, len(batch), sym, e)

            if i < len(batch) - 1:
                time.sleep(DELAY_SEC)

    result = {
        "ok": True,
        "hour_idx": hour_idx,
        "batch_hours": BATCH_HOURS,
        "slice_start": start,
        "slice_end": end,
        "batch_size": len(batch),
        "total_universe": len(symbols),
        "processed_ok": ok_count,
        "errors": err_count,
        "rate_limited": rate_limited,
        "analysis_url": ANALYSIS_URL,
    }
    logger.info("Hydration done: %s", result)
    return result


if __name__ == "__main__":
    import sys

    idx = None
    if len(sys.argv) > 1:
        try:
            idx = int(sys.argv[1])
        except ValueError:
            pass
    out = hydrate_batch(hour_idx=idx)
    # Non-zero exit if nothing succeeded and we had work to do
    if out.get("batch_size", 0) > 0 and out.get("processed_ok", 0) == 0:
        sys.exit(1)
    sys.exit(0)
