"""
Refill Additional Data — manual + programmatic hydration of slow fields
(fundamentals, technical snapshot, events) into the data-feed store.

Runs as a background job on the API gateway so the Data Feed UI button can
trigger it without blocking. Uses the same merge-never-wipe put_symbol path.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("refill-additional")

IST = timezone(timedelta(hours=5, minutes=30))

_AI = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_AI}/fundamental").rstrip("/")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_AI}/technical").rstrip("/")
EVENT_URL = os.getenv("EVENT_URL", f"{_AI}/event").rstrip("/")

DELAY_SEC = float(os.getenv("REFILL_DELAY_SEC", os.getenv("HYDRATOR_DELAY_SEC", "18.0")))
REQUEST_TIMEOUT = float(os.getenv("REFILL_TIMEOUT_SEC", os.getenv("HYDRATOR_TIMEOUT_SEC", "90.0")))
MAX_SYMBOLS = int(os.getenv("REFILL_MAX_SYMBOLS", "0") or 0)  # 0 = full universe

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
    return dict(_REFILL_JOB)


def _set_job(**kw) -> dict:
    _REFILL_JOB.update(kw)
    _REFILL_JOB["updated_at"] = datetime.now(IST).isoformat()
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
    for attempt in range(3):
        try:
            r = client.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code == 200 and r.content:
                data = r.json()
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug("GET %s: %s", url, e)
            time.sleep(8 * (attempt + 1))
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


def run_refill_additional(symbols: Optional[List[str]] = None) -> dict:
    """Blocking worker — call from BackgroundTasks or CLI."""
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
    if MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]

    total = len(symbols)
    ok_n = 0
    err_n = 0
    _set_job(
        status="running",
        message=f"Refill Additional Data: 0/{total}…",
        processed=0,
        total=total,
        ok_count=0,
        error_count=0,
        started_at=datetime.now(IST).isoformat(),
        stop_requested=False,
    )

    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for i, sym in enumerate(symbols):
            if data_feed_stop_requested():
                _set_job(
                    status="stopped",
                    message=f"Stopped at {i}/{total}",
                    processed=i,
                    ok_count=ok_n,
                    error_count=err_n,
                )
                return get_refill_job()

            try:
                fund = _get_json(client, f"{FUNDAMENTAL_URL}/analyze/{sym}?force=true")
                tech = _get_json(client, f"{TECHNICAL_URL}/analyze/{sym}?force=true")
                events = _get_json(client, f"{EVENT_URL}/events/{sym}?force=true")
                row = _build_payload(sym, fund, tech, events)
                if len(row) > 2:
                    store.put_symbol(sym, row)
                    ok_n += 1
                else:
                    err_n += 1
            except Exception as e:
                err_n += 1
                logger.warning("refill %s: %s", sym, e)

            processed = i + 1
            if processed % 3 == 0 or processed == total:
                _set_job(
                    status="running",
                    message=f"Refill Additional Data: {processed}/{total} (ok={ok_n} err={err_n})",
                    processed=processed,
                    total=total,
                    ok_count=ok_n,
                    error_count=err_n,
                )

            if i < total - 1:
                time.sleep(DELAY_SEC)

    return _set_job(
        status="done",
        message=f"Refill complete: {ok_n}/{total} ok, {err_n} errors",
        processed=total,
        total=total,
        ok_count=ok_n,
        error_count=err_n,
    )
