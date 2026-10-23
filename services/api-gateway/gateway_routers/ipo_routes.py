"""IPO scan / list / audit / add / repair / purge routes (each also under /surprise/ipo/...). Moved out of main.py in E52, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("api-gateway")

ipo_scan_router = APIRouter()


@ipo_scan_router.post("/ipo/scan")
@ipo_scan_router.get("/ipo/scan")
@ipo_scan_router.post("/surprise/ipo/scan")
@ipo_scan_router.get("/surprise/ipo/scan")
async def api_ipo_scan(
    background: bool = Query(True),
    force: bool = Query(
        False,
        description=(
            "Bypass the ipo_static_feed 24h freshness cache. Defaults to "
            "False: the 'IPO Premarket Refresh' GitHub Action "
            "(.github/workflows/ipo-premarket.yml) runs a full discovery + "
            "scoring pass every trading morning before the session opens, "
            "so a normal 'Scan IPOs' click should read that already-fresh "
            "table (near-instant) rather than re-discovering NSE + "
            "re-pulling yfinance history for the whole universe in front "
            "of the user. The frontend's 'Force Rescan' button passes "
            "force=true explicitly when a real on-demand re-scan is wanted."
        ),
    ),
    wipe: bool = Query(
        False,
        description=(
            "Delete every existing ipo_static_feed row (and replace, not "
            "merge, the cached list) once the fresh upstream fetch has been "
            "confirmed healthy enough to trust — see the safety check in "
            "ipo_scanner._run_ipo_scan_locked. Implies force=true. The "
            "frontend's '📥 Premarket Feed' button passes this so stale/"
            "removed IPOs and any leftover non-equity rows don't just "
            "accumulate forever; 'Force Rescan' does NOT pass this, so it "
            "stays a safe merge-only refresh."
        ),
    ),
    background_tasks: BackgroundTasks = None,
):
    """
    Recent IPO scanner — Surprise tab subsection. Scores recently-listed
    (and listing-today) NSE IPOs for a short-term buy/sell decision using
    ipo_scanner.analyze_ipo(). Runs in the background like the premarket
    job; poll /surprise/ipo/status, then GET /surprise/ipo/list.
    """
    try:
        from ipo_scanner import run_ipo_scan, get_ipo_scan_progress
    except Exception as e:
        # Same shape as the sibling IPO routes when ipo_scanner can't load.
        return {"accepted": False, "status": "error", "error": f"ipo_scanner unavailable: {str(e)[:160]}"}

    current = get_ipo_scan_progress()
    if current.get("status") == "running":
        return {"accepted": True, "already_running": True, **current}

    if background and background_tasks is not None:
        background_tasks.add_task(run_ipo_scan, force=force, wipe=wipe)
        return {"accepted": True, "background": True, "force": force, "wipe": wipe, "message": "IPO scan started"}
    try:
        result = await asyncio.to_thread(run_ipo_scan, force=force, wipe=wipe)  # group361: off the event loop
    except Exception as e:
        logger.exception("ipo/scan failed: %s", e)
        return {"accepted": False, "background": False, "status": "error", "error": str(e)[:200]}
    if not isinstance(result, dict):
        result = {}
    # The envelope keys are spread LAST so a scan result carrying its own
    # ``accepted`` / ``background`` can no longer override them.
    return {**result, "accepted": True, "background": False}


ipo_list_router = APIRouter()


@ipo_list_router.get("/ipo/list")
@ipo_list_router.get("/surprise/ipo/list")
async def api_ipo_list(display_days: Optional[int] = Query(None)):
    """Current analyzed IPO list — listing-today/pre-listing first, then by
    score. Each row includes a ready-to-use `buy_suggestion` (same shape as
    /api/scan/find-buys) for BUY NOW / PREPARE TO BUY rows so the frontend
    can open the existing BuySniperModal directly.

    display_days optionally narrows the (up to ~1 year wide) scanned universe
    down to listings within the last N days for display, without re-scanning
    — defaults to ~30 days (IPO_CHECKER_DEFAULT_DISPLAY_DAYS). Pass a large
    value (e.g. 365) to see everything the scan actually found.

    BUG FIX (2026-09-12): get_ipo_list() does a synchronous kv_get(), which on
    a cache-miss falls through to a blocking Oracle/Neon round trip. Calling
    that directly inside this `async def` route blocked api-gateway's whole
    (single-worker) event loop for as long as the DB call took/hung — every
    other route queued behind it. Now bounded by oracle_compat's
    ORACLE_CALL_TIMEOUT_MS AND offloaded to a worker thread via
    run_in_threadpool, so a slow/stuck DB round trip no longer stalls other
    requests; the existing except below still guarantees a clean fallback
    response either way."""
    try:
        from ipo_scanner import get_ipo_list
        return await run_in_threadpool(get_ipo_list, display_days=display_days)
    except Exception as e:
        return {"results": [], "generated_at": None, "error": str(e)[:160]}


ipo_audit_router = APIRouter()


@ipo_audit_router.get("/ipo/audit")
@ipo_audit_router.get("/surprise/ipo/audit")
async def api_ipo_audit():
    """
    IPO Tracker's OWN feed-health audit (reads ipo_static_feed) — NOT the
    general stock-universe feed. This is what the IPO Tracker tab's
    Database health widget should call; it previously called the shared
    /api/feed/audit-missing (general stock feed), which is why the IPO tab
    showed unrelated stock symbols instead of IPO rows/scores.
    """
    try:
        from ipo_scanner import get_ipo_feed_audit
        # Same fix as /surprise/ipo/list — offload the blocking DB-backed call
        # so it can't stall the shared event loop (see 2026-09-12 note there).
        return await run_in_threadpool(get_ipo_feed_audit)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}


ipo_add_router = APIRouter()


class IpoAddRequest(BaseModel):
    company_name: str


@ipo_add_router.post("/ipo/add")
@ipo_add_router.post("/surprise/ipo/add")
async def api_ipo_add(body: IpoAddRequest):
    """Manually register an IPO by NAME ONLY — the reliable path since
    NSE's IPO API blocks cloud-hosted IPs often enough that auto-discovery
    alone can't be the only path. Resolves symbol/issue price/listing date
    automatically (see ipo_scanner.add_manual_ipo_by_name) against NSE's
    calendar and, as a fallback, ipoalerts — the user never has to look up
    or type those themselves. A resolved entry is persisted as a manual
    IPO and included in every subsequent scan/list the same way any other
    manual entry is."""
    try:
        from ipo_scanner import add_manual_ipo_by_name
        result = await asyncio.to_thread(add_manual_ipo_by_name, body.company_name)  # group361: off the event loop
    except Exception as e:
        logger.exception("ipo/add failed: %s", e)
        return {"accepted": False, "message": f"IPO lookup failed: {str(e)[:160]}", "suggestions": []}
    if not isinstance(result, dict):
        result = {}
    if not result.get("resolved"):
        return {
            "accepted": False,
            "message": result.get("message"),
            "suggestions": result.get("suggestions") or [],
        }
    return {"accepted": True, "entry": result.get("entry")}


ipo_repair_router = APIRouter()


@ipo_repair_router.post("/ipo/repair-batch")
@ipo_repair_router.post("/surprise/ipo/repair-batch")
async def api_ipo_repair_batch(limit: int = Query(15, ge=1, le=100), symbol: Optional[str] = Query(None)):
    """Targeted repair for ipo_static_feed rows missing price/score/decision
    — re-runs analyze_ipo() only for the specific missing symbols (bounded
    by `limit`), not a full-universe re-scan. Mirrors /stockky-hot/repair-batch
    and /api/surprise/repair-batch's naming and shape; backs the IPO Tracker
    health tab's Auto-Repair button."""
    try:
        from ipo_scanner import ipo_repair_batch
    except Exception as e:
        return {"status": "error", "error": f"ipo_scanner unavailable: {str(e)[:160]}"}
    # AUDIT FIX: same blocking-event-loop bug as /api/surprise/repair-batch
    # above — ipo_repair_batch() is sync and loops up to `limit` (100 max)
    # symbols with a time.sleep(0.3) pace-out between each; called directly
    # from this async route it blocked every other api-gateway request for
    # the full repair duration. Offload to a worker thread.
    try:
        return await asyncio.to_thread(ipo_repair_batch, limit=limit, symbol=symbol)
    except Exception as e:
        logger.exception("ipo/repair-batch failed: %s", e)
        return {"status": "error", "error": str(e)[:160]}


ipo_purge_router = APIRouter()


@ipo_purge_router.post("/ipo/purge-non-equity")
@ipo_purge_router.post("/surprise/ipo/purge-non-equity")
async def api_ipo_purge_non_equity():
    """Delete every ipo_static_feed row flagged non_equity (NCD/bond
    debt-series symbols like 1150VIES30 that slipped in before
    ipo_scanner._is_ipo_non_equity existed) — these can never resolve via
    Auto-Repair since they were never real equity IPOs to begin with.
    Backs the IPO health tab's 'Delete Non-Equity Rows' button."""
    try:
        from ipo_scanner import purge_non_equity_ipos
    except Exception as e:
        return {"status": "error", "error": f"ipo_scanner unavailable: {str(e)[:160]}"}
    try:
        return await asyncio.to_thread(purge_non_equity_ipos)  # group361: off the event loop
    except Exception as e:
        logger.exception("ipo/purge-non-equity failed: %s", e)
        return {"status": "error", "error": str(e)[:160]}
