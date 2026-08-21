"""
Refill Additional Data — manual + programmatic hydration of slow fields
(fundamentals, technical snapshot, events) into the data-feed store.

Runs as a background job on the API gateway so the Data Feed UI button can
trigger it without blocking. Uses the same merge-never-wipe put_symbol path.

Fix: the previous version processed symbols one at a time with an 18s fixed
sleep and up to 90s x 3 retries per endpoint. Worst case that's >10 minutes
per symbol, and even the happy path (~300 symbols x ~20-30s) is 1.5-2+
hours — long enough that a free-tier dyno idling out (or a restart) kills
the background thread mid-run, leaving the job stuck at status="running"
forever with no way to tell it's actually dead. Now uses bounded concurrency
(ThreadPoolExecutor, matching the pattern the main /data-feed/run fundamentals
phase already uses successfully) with shorter per-request timeouts and a
single attempt (fast-fail) instead of blind retries, plus a staleness check
so the UI can detect and recover from a dead job instead of spinning forever.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("refill-additional")

IST = timezone(timedelta(hours=5, minutes=30))

_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_AI}/fundamental").rstrip("/")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI}/technical").rstrip("/")
EVENT_URL = os.getenv("EVENT_URL", f"{_AI}/event").rstrip("/")

# Per-request timeout — short and single-attempt so one slow symbol can't
# stall the whole job. Concurrency (below) is what gives real throughput,
# not long per-request waits.
REQUEST_TIMEOUT = float(os.getenv("REFILL_TIMEOUT_SEC", "25.0"))
# Bounded concurrency — mirrors DATA_FEED_FUND_CONCURRENCY used by the main
# /data-feed/run fundamentals phase, so we don't cause a fresh 429 storm.
CONCURRENCY = int(os.getenv("REFILL_CONCURRENCY", "4"))
MAX_SYMBOLS = int(os.getenv("REFILL_MAX_SYMBOLS", "0") or 0)  # 0 = full universe
# If a "running" job hasn't updated in this long, treat it as dead (Render
# free-tier idle-kill or crash) so the UI can recover instead of spinning.
STALE_AFTER_SEC = float(os.getenv("REFILL_STALE_AFTER_SEC", "300"))

# Process-local job mirror (status also written to data_feed job when available)
_REFILL_JOB: Dict[str, Any] = {
    "status": "idle",
    "message": "Idle",
    "processed": 0,
    "total": 0,
    "ok_count": 0,
    "error_count": 0,
    "kind": "refill_additional",
}


def get_refill_job() -> dict:
    job = dict(_REFILL_JOB)
    if job.get("status") == "running":
        try:
            last = job.get("_updated_epoch") or 0
            if last and (time.time() - last) > STALE_AFTER_SEC:
                job["status"] = "stalled"
                job["message"] = (
                    f"No progress for {int(time.time() - last)}s — job likely died "
                    f"(dyno idle-kill/restart). Safe to start again."
                )
        except Exception:
            pass
    return job


def _set_job(**kw) -> dict:
    _REFILL_JOB.update(kw)
    _REFILL_JOB["updated_at"] = datetime.now(IST).isoformat()
    _REFILL_JOB["_updated_epoch"] = time.time()
    # Best-effort mirror into data-feed job store for UI polling compatibility
    try:
        from data_feed import get_data_feed_store

        store = get_data_feed_store()
        store.set_job(
            status=kw.get("status", _REFILL_JOB.get("status")),
            message=kw.get("message", _REFILL_JOB.get("message")),
            processed=kw.get("processed", _REFILL_JOB.get("processed")),
            total=kw.get("total", _REFILL_JOB.get("total")),
            ok_count=kw.get("ok_count", _REFILL_JOB.get("ok_count")),
            error_count=kw.get("error_count", _REFILL_JOB.get("error_count")),
            kind="refill_additional",
            updated_at=_REFILL_JOB["updated_at"],
        )
    except Exception as e:
        logger.debug("refill job mirror: %s", e)
    return dict(_REFILL_JOB)


def _norm(sym: str) -> str:
    return str(sym or "").strip().upper().replace(".NS", "").replace(".BO", "")


def _get_json(client: httpx.Client, url: str) -> Optional[dict]:
    """Single attempt + one short retry on 429 only. Concurrency (not long
    per-request retry chains) is what gets us through the universe in a
    reasonable time — a symbol that fails just falls through to error_count
    instead of eating minutes of wall-clock.

    Also gated behind the shared rate limiter (rate_limiter.py) — refill
    additional, weekend hydrator, market scan, and repair buttons can all
    hit analysis-intelligence-service concurrently, so this acquires a
    slot on a shared "analysis" bucket first, and widens its own timeout
    if that bucket is already busy (queued behind other jobs) instead of
    timing out into congestion it can't see."""
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
        logger.debug("GET %s: %s", url, e)
    return None


def _build_payload(sym: str, fund: Optional[dict], tech: Optional[dict], events: Optional[dict]) -> dict:
    row: Dict[str, Any] = {"symbol": sym, "source": "refill_additional"}
    metrics = (fund or {}).get("metrics") if isinstance((fund or {}).get("metrics"), dict) else {}
    for key in (
        "pe_ratio", "roe", "roce", "debt_to_equity", "revenue_growth", "market_cap",
        "sector", "industry", "quality_score", "fundamental_score", "promoter_holding",
        "eps", "book_value", "dividend_yield", "forward_pe", "earnings_growth",
    ):
        val = (fund or {}).get(key)
        if val is None:
            val = metrics.get(key)
        if val is not None:
            row[key] = val
            row[f"{key}_seed"] = False
    if fund and fund.get("summary"):
        row["fundamental_summary"] = fund.get("summary")
    for key in ("technical_score", "rsi", "trend_strength", "volume_surge", "support", "resistance", "close"):
        if tech and tech.get(key) is not None:
            row[key] = tech.get(key)
    if events:
        for key in ("next_earnings_date", "earnings_surprise", "event_summary", "has_positive_catalyst", "recent_event_score"):
            if events.get(key) is not None:
                row[key] = events.get(key)
        if events.get("summary") and "event_summary" not in row:
            row["event_summary"] = events.get("summary")
    return row


def _hydrate_one(client: httpx.Client, sym: str) -> tuple[str, bool]:
    fund = _get_json(client, f"{FUNDAMENTAL_URL}/analyze/{sym}?force=true")
    tech = _get_json(client, f"{TECHNICAL_URL}/analyze/{sym}?force=true")
    events = _get_json(client, f"{EVENT_URL}/events/{sym}?force=true")
    row = _build_payload(sym, fund, tech, events)
    return sym, row


def run_refill_additional(symbols: Optional[List[str]] = None) -> dict:
    """Blocking worker — call from BackgroundTasks or CLI.

    Bounded-concurrency (CONCURRENCY workers) instead of one-symbol-at-a-time
    with an 18s sleep — that sequential version could take 1.5-2+ hours for a
    ~300 symbol universe, well past a free-tier dyno's idle-kill window, so
    the job would die mid-run and get stuck at status="running" forever.
    """
    from data_feed import get_data_feed_store, data_feed_stop_requested, clear_data_feed_stop

    store = get_data_feed_store()
    clear_data_feed_stop()

    if not symbols:
        try:
            symbols = list(store.list_symbols() or [])
        except Exception:
            symbols = []
    if not symbols:
        # Fallback: empty list is error
        return _set_job(status="error", message="No symbols to refill", processed=0, total=0)

    symbols = [_norm(s) for s in symbols if _norm(s)]
    seen = set()
    symbols = [s for s in symbols if not (s in seen or seen.add(s))]
    if MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]

    total = len(symbols)
    ok_n = 0
    err_n = 0
    processed = 0
    _set_job(
        status="running",
        message=f"Refill Additional Data: 0/{total} (concurrency={CONCURRENCY})…",
        processed=0,
        total=total,
        ok_count=0,
        error_count=0,
        started_at=datetime.now(IST).isoformat(),
        stop_requested=False,
    )

    limits = httpx.Limits(max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY)
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, limits=limits) as client:
        with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as pool:
            futures = {pool.submit(_hydrate_one, client, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                if data_feed_stop_requested():
                    for f in futures:
                        f.cancel()
                    return _set_job(
                        status="stopped",
                        message=f"Stopped at {processed}/{total}",
                        processed=processed,
                        ok_count=ok_n,
                        error_count=err_n,
                    )

                try:
                    _, row = fut.result()
                    if len(row) > 2:
                        store.put_symbol(sym, row)
                        ok_n += 1
                    else:
                        err_n += 1
                except Exception as e:
                    err_n += 1
                    logger.warning("refill %s: %s", sym, e)

                processed += 1
                if processed % 3 == 0 or processed == total:
                    _set_job(
                        status="running",
                        message=f"Refill Additional Data: {processed}/{total} (ok={ok_n} err={err_n})",
                        processed=processed,
                        total=total,
                        ok_count=ok_n,
                        error_count=err_n,
                    )

    return _set_job(
        status="done",
        message=f"Refill complete: {ok_n}/{total} ok, {err_n} errors",
        processed=total,
        total=total,
        ok_count=ok_n,
        error_count=err_n,
    )