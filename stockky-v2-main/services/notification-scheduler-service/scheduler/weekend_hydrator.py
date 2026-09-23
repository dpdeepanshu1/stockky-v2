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
import threading
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


def _check_regime_constant_staleness() -> None:
    """Log a warning when any regime-dependent trading constant is stale (>30 days).
    Called at hydrator start so the warning appears in scheduler logs weekly."""
    from datetime import datetime, timezone
    REGIME_CONSTANTS = {
        "ENTRY_REGIME_MIN_SCORE":     ("38",   "2026-08-28"),
        "ENTRY_MIN_REWARD_RISK":      ("2.0",  "2026-08-28"),
        "CANDIDATE_MIN_CONVICTION":   ("55",   "2026-08-28"),
        "CANDIDATE_DOWNTREND_6M_PCT": ("-10.0","2026-08-28"),
    }
    now = datetime.now(timezone.utc)
    for name, (val, reviewed) in REGIME_CONSTANTS.items():
        try:
            age = (now - datetime.strptime(reviewed, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
            if age >= 30:
                logger.warning(
                    "STALE REGIME CONSTANT: %s=%s (last reviewed %s, %dd ago). "
                    "Re-run market research if market regime has changed.",
                    name, val, reviewed, age
                )
        except Exception:
            pass


# Sector priority for hydration: outperforming sectors first (Aug-2026 research).
# Symbols from these sectors will be hydrated in the first batch pass.
_PRIORITY_SECTORS = frozenset({
    "bank", "banking", "psu", "auto", "automobile", "metal", "steel",
    "private bank", "nbfc", "finance",
})


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
    comment above for why long retry chains were removed. Also gated
    behind the shared rate limiter (rate_limiter.py, shared "analysis"
    bucket) so this doesn't stack with refill_additional/market-scan hits
    against the same upstream service."""
    try:
        from rate_limiter import acquire as rl_acquire, suggested_timeout as rl_timeout
        rl_acquire("analysis", weight=1)
        timeout = rl_timeout(REQUEST_TIMEOUT, "analysis")
    except Exception:
        timeout = REQUEST_TIMEOUT
    try:
        r = client.get(url, timeout=timeout)
        if r.status_code == 429:
            time.sleep(5)
            r = client.get(url, timeout=timeout)
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


# ── Background job wrapper ──────────────────────────────────────────────
# Fix: /scheduler/hydrate/weekend used to run hydrate_batch() synchronously
# inside the request handler. A real slice (or --full) can legitimately take
# minutes to hours (see GHA workflow, which uses a 900s curl timeout for a
# single slice), so any normal HTTP client/proxy/test harness with a shorter
# timeout (load balancers, uptime checks, CI smoke tests) sees the connection
# die mid-flight with no response at all. The work itself was never the bug —
# doing it on the request thread was. Mirrors the same fire-and-forget +
# pollable /status pattern already used by refill_additional.py on the
# api-gateway, so callers that want to wait can poll instead of holding a
# socket open for the whole run.
_HYDRATE_JOB: dict[str, Any] = {
    "status": "idle",
    "message": "Idle",
    "full": False,
    "hour_idx": None,
    "processed_ok": 0,
    "errors": 0,
    "batch_size": 0,
    "total_universe": 0,
    "started_epoch": None,
    "updated_epoch": None,
}
_HYDRATE_LOCK = threading.Lock()
_STALE_AFTER_SEC = float(os.getenv("HYDRATOR_STALE_AFTER_SEC", "1800"))


def get_hydrate_job() -> dict:
    with _HYDRATE_LOCK:
        job = dict(_HYDRATE_JOB)
    if job.get("status") == "running":
        last = job.get("updated_epoch") or 0
        if last and (time.time() - last) > _STALE_AFTER_SEC:
            job["status"] = "stalled"
            job["message"] = (
                f"No progress for {int(time.time() - last)}s — job likely died "
                f"(dyno idle-kill/restart). Safe to start again."
            )
    return job


def _set_job(**kw) -> None:
    with _HYDRATE_LOCK:
        _HYDRATE_JOB.update(kw)
        _HYDRATE_JOB["updated_epoch"] = time.time()


def _run_job(hour_idx: Optional[int], full: bool) -> None:
    _set_job(
        status="running",
        message="Hydrating…",
        full=full,
        hour_idx=hour_idx,
        started_epoch=time.time(),
    )
    try:
        result = hydrate_batch(hour_idx=hour_idx, full=full)
        _set_job(
            status="done" if result.get("ok") else "error",
            message="Hydration complete" if result.get("ok") else str(result.get("error")),
            processed_ok=result.get("processed_ok", 0),
            errors=result.get("errors", 0),
            batch_size=result.get("batch_size", 0),
            total_universe=result.get("total_universe", 0),
            hour_idx=result.get("hour_idx", hour_idx),
        )
    except Exception as e:
        logger.exception("hydrate background job failed")
        _set_job(status="error", message=str(e)[:300])


def start_hydrate_background(hour_idx: Optional[int] = None, full: bool = False) -> dict:
    """Kick off hydrate_batch() on a daemon thread and return immediately.

    Refuses to start a second run on top of one already in progress (unless
    the previous run has gone stale) so concurrent triggers don't double up
    on rate-limited upstream calls.
    """
    current = get_hydrate_job()
    if current.get("status") == "running":
        return {"ok": True, "already_running": True, "job": current}
    thread = threading.Thread(target=_run_job, args=(hour_idx, full), daemon=True)
    thread.start()
    return {"ok": True, "started": True, "job": get_hydrate_job()}


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
