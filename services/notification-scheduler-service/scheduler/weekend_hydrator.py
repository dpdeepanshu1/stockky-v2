"""
Weekend / off-hours additional-data hydrator (fundamentals + technical + events).

Time-sliced across HYDRATOR_BATCH_HOURS (default 48) so hourly cron can warm the
full universe without rate-limit storms. Manual / full mode processes everything
with long timeouts and generous inter-symbol delays.

Usage:
  python weekend_hydrator.py              # current hour slice
  python weekend_hydrator.py 3            # slice 3
  python weekend_hydrator.py --full       # entire universe (slow, intentional)
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weekend-hydrator")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://localhost:8000").rstrip("/")
_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_AI}/fundamental").rstrip("/")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI}/technical").rstrip("/")
EVENT_URL = os.getenv("EVENT_URL", f"{_AI}/event").rstrip("/")

# Fix: the old sequential loop (one symbol at a time, 18s fixed sleep, up to
# 2 retries x 90s timeout per endpoint) could take >10 min/symbol in the
# worst case and 1.5-2+ hours even in the happy path for a few hundred
# symbols — long past a free/cron worker's realistic run window, so batches
# routinely never finished. Bounded concurrency (like the API gateway's
# refill_additional.py) gets the same work done in minutes, not hours.
BATCH_HOURS = int(os.getenv("HYDRATOR_BATCH_HOURS", "48"))
REQUEST_TIMEOUT = float(os.getenv("HYDRATOR_TIMEOUT_SEC", "25.0"))
PERSIST_TIMEOUT = float(os.getenv("HYDRATOR_PERSIST_TIMEOUT_SEC", "20.0"))
CONCURRENCY = int(os.getenv("HYDRATOR_CONCURRENCY", "4"))
# Kept for backward-compat env overrides; no longer used as a per-symbol sleep.
DELAY_SEC = float(os.getenv("HYDRATOR_DELAY_SEC", "0"))


def _fetch_universe() -> list[str]:
    urls = [
        f"{API_GATEWAY_URL}/scan/universe",
        f"{API_GATEWAY_URL}/api/universe",
        f"{API_GATEWAY_URL}/universe",
    ]
    for url in urls:
        try:
            resp = httpx.get(url, timeout=60.0)
            if resp.status_code != 200:
                continue
            data = resp.json() if resp.content else {}
            if isinstance(data, list):
                symbols = data
            elif isinstance(data, dict):
                symbols = data.get("symbols") or data.get("universe") or data.get("tickers") or []
            else:
                symbols = []
            out = sorted(
                {
                    str(s).strip().upper().replace(".NS", "").replace(".BO", "")
                    for s in symbols
                    if s
                }
            )
            if out:
                logger.info("Universe from %s: %d symbols", url, len(out))
                return out
        except Exception as e:
            logger.debug("Universe fetch %s failed: %s", url, e)
    logger.warning("No universe available — empty batch")
    return []


def _get_json(client: httpx.Client, url: str) -> Optional[dict]:
    """Single attempt + one short retry on 429 only — see module docstring
    comment above for why long retry chains were removed."""
    try:
        r = client.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            time.sleep(5)
            r = client.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and r.content:
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("GET failed %s: %s", url, str(e)[:120])
    return None


def _extract_fund_row(sym: str, analysis: dict) -> dict[str, Any]:
    metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
    row: dict[str, Any] = {"symbol": sym, "source": "weekend_hydrator"}
    for key in (
        "pe_ratio", "roe", "roce", "debt_to_equity", "revenue_growth",
        "market_cap", "sector", "industry", "quality_score", "fundamental_score",
        "promoter_holding", "eps", "book_value", "dividend_yield",
        "forward_pe", "earnings_growth", "current_ratio",
    ):
        val = analysis.get(key)
        if val is None:
            val = metrics.get(key)
        if val is not None:
            row[key] = val
            row[f"{key}_seed"] = False
    if analysis.get("summary"):
        row["fundamental_summary"] = analysis.get("summary")
    return row


def _extract_tech_fields(analysis: dict) -> dict[str, Any]:
    if not isinstance(analysis, dict):
        return {}
    out = {}
    for key in (
        "technical_score", "rsi", "trend_strength", "volume_surge",
        "support", "resistance", "close", "price", "volume_ratio",
    ):
        if analysis.get(key) is not None:
            out[key] = analysis.get(key)
    return out


def _extract_event_fields(analysis: dict) -> dict[str, Any]:
    if not isinstance(analysis, dict):
        return {}
    out = {}
    for key in (
        "next_earnings_date", "earnings_surprise", "event_summary",
        "has_positive_catalyst", "recent_event_score", "count", "total",
    ):
        if analysis.get(key) is not None:
            out[key if key not in ("count", "total") else "events_count"] = analysis.get(key)
    if analysis.get("summary") and "event_summary" not in out:
        out["event_summary"] = analysis.get("summary")
    return out


def _persist(client: httpx.Client, row: dict) -> bool:
    if not row or len(row) <= 2:
        return False
    try:
        pr = client.post(
            f"{API_GATEWAY_URL}/data-feed/update",
            json=row,
            timeout=PERSIST_TIMEOUT,
        )
        return pr.status_code < 400
    except Exception as e:
        logger.debug("persist %s failed: %s", row.get("symbol"), e)
        return False


def hydrate_symbol(client: httpx.Client, sym: str) -> dict[str, Any]:
    """Force-refresh fundamental + technical + events and merge into data-feed."""
    result = {"symbol": sym, "ok": False, "parts": []}
    row: dict[str, Any] = {"symbol": sym, "source": "weekend_hydrator"}

    fund = _get_json(client, f"{FUNDAMENTAL_URL}/analyze/{sym}?force=true")
    if fund:
        row.update(_extract_fund_row(sym, fund))
        result["parts"].append("fundamental")

    tech = _get_json(client, f"{TECHNICAL_URL}/analyze/{sym}?force=true")
    if tech:
        row.update(_extract_tech_fields(tech))
        result["parts"].append("technical")

    events = _get_json(client, f"{EVENT_URL}/events/{sym}?force=true")
    if events:
        row.update(_extract_event_fields(events))
        result["parts"].append("events")

    if _persist(client, row):
        result["ok"] = True
        result["parts"].append("persisted")
    elif result["parts"]:
        # Analysis warmed caches even if persist failed
        result["ok"] = True
        result["parts"].append("cache_only")
    return result


def hydrate_batch(
    hour_idx: Optional[int] = None,
    full: bool = False,
    symbols: Optional[list[str]] = None,
) -> dict:
    """
    Hydrate one time-slice (or full universe).
    full=True ignores hour slicing (for manual Refill Additional Data / GHA full pass).
    """
    all_symbols = symbols if symbols is not None else _fetch_universe()
    if not all_symbols:
        return {"ok": False, "error": "empty_universe", "processed": 0, "total": 0}

    if full:
        batch = list(all_symbols)
        hour_idx = -1
        start, end = 0, len(batch)
    else:
        if hour_idx is None:
            hour_idx = int(time.time() / 3600) % BATCH_HOURS
        hour_idx = max(0, min(int(hour_idx), BATCH_HOURS - 1))
        chunk_size = max(1, math.ceil(len(all_symbols) / BATCH_HOURS))
        start = hour_idx * chunk_size
        end = min(start + chunk_size, len(all_symbols))
        batch = all_symbols[start:end]

    logger.info(
        "Hydrating %s: %d symbols (delay=%.1fs timeout=%.0fs)",
        "FULL" if full else f"slice {hour_idx + 1}/{BATCH_HOURS} [{start}:{end}]",
        len(batch),
        DELAY_SEC,
        REQUEST_TIMEOUT,
    )

    ok_count = 0
    err_count = 0
    rate_limited = False

    limits = httpx.Limits(max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY)
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, limits=limits) as client:
        with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as pool:
            futures = {pool.submit(hydrate_symbol, client, sym): sym for sym in batch}
            done_n = 0
            for fut in as_completed(futures):
                sym = futures[fut]
                done_n += 1
                try:
                    r = fut.result()
                    if r.get("ok"):
                        ok_count += 1
                        logger.info("[%d/%d] %s OK (%s)", done_n, len(batch), sym, ",".join(r.get("parts") or []))
                    else:
                        err_count += 1
                        logger.warning("[%d/%d] %s no data", done_n, len(batch), sym)
                except Exception as e:
                    err_count += 1
                    msg = str(e)
                    if "429" in msg:
                        rate_limited = True
                    logger.error("[%d/%d] Error %s: %s", done_n, len(batch), sym, e)

    result = {
        "ok": True,
        "full": full,
        "hour_idx": hour_idx,
        "batch_hours": BATCH_HOURS,
        "slice_start": start,
        "slice_end": end,
        "batch_size": len(batch),
        "total_universe": len(all_symbols),
        "processed_ok": ok_count,
        "errors": err_count,
        "rate_limited": rate_limited,
        "delay_sec": DELAY_SEC,
        "timeout_sec": REQUEST_TIMEOUT,
        "fundamental_url": FUNDAMENTAL_URL,
    }
    logger.info("Hydration done: %s", result)
    return result


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a]
    full = "--full" in args or "full" in args
    args = [a for a in args if a not in ("--full", "full")]
    idx = None
    if args:
        try:
            idx = int(args[0])
        except ValueError:
            pass
    out = hydrate_batch(hour_idx=idx, full=full)
    if out.get("batch_size", 0) > 0 and out.get("processed_ok", 0) == 0:
        sys.exit(1)
    sys.exit(0)
