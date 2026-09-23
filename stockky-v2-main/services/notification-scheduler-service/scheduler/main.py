"""
Lightweight scheduler module mounted at /scheduler.

Primary duty on free-tier Render: keep Neon warm with SELECT 1 every ~4 minutes
so market scans avoid cold-start lag. External GitHub Actions can also hit
the gateway `/ops/neon-keepalive` endpoint; this is the in-process safety net.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import FastAPI

logger = logging.getLogger("scheduler")

app = FastAPI(title="Stockky Scheduler (merged)", version="0.1.0")

_NEON_INTERVAL = int(os.getenv("NEON_KEEPALIVE_INTERVAL_SEC", "240"))
_task: Optional[asyncio.Task] = None


def _select_1() -> dict:
    try:
        # Neon free-tier keep-alive only; Oracle has no auto-suspend to prevent
        # and needs "FROM dual", so skip cleanly. Guard is False on Render/Neon.
        if os.environ.get("ORACLE_DSN"):
            return {"ok": True, "source": "oracle-skip"}
        url = (
            os.getenv("CACHE_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("TRAINING_DATABASE_URL")
        )
        if not url:
            return {"ok": False, "error": "no_database_url"}
        if url.lower().startswith("oracle"):
            return {"ok": True, "source": "oracle-skip"}
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/health")
def health():
    return {"status": "ok", "service": "scheduler", "neon_interval_sec": _NEON_INTERVAL}


@app.get("/neon-keepalive")
@app.post("/neon-keepalive")
def neon_keepalive():
    return _select_1()



@app.get("/hydrate/weekend")
@app.post("/hydrate/weekend")
def hydrate_weekend(hour_idx: int | None = None, full: bool = False, wait: bool = False):
    """
    Time-sliced weekend hydration (force=true) for fundamentals + technical + events.
    hour_idx 0..47 selects the slice; omit to use current UTC hour % 48.
    full=true processes the entire universe (manual / GHA full pass — slow by design).

    Runs in the background and returns immediately by default (poll
    /hydrate/weekend/status for progress) since a real slice — let alone a
    full pass — can take from minutes to hours, far past what any normal
    HTTP client/proxy/health-check timeout will tolerate on an open socket.
    Pass wait=true to block and return the final result inline instead
    (only recommended for manual runs with a very long client-side timeout).
    """
    try:
        from weekend_hydrator import hydrate_batch, start_hydrate_background
        if wait:
            return hydrate_batch(hour_idx=hour_idx, full=full)
        return start_hydrate_background(hour_idx=hour_idx, full=full)
    except Exception as e:
        logger.exception("hydrate_weekend failed")
        return {"ok": False, "error": str(e)[:300]}


@app.get("/hydrate/weekend/status")
def hydrate_weekend_status():
    """Poll the state of the current/last background hydration job."""
    try:
        from weekend_hydrator import get_hydrate_job
        return get_hydrate_job()
    except Exception as e:
        logger.exception("hydrate_weekend_status failed")
        return {"ok": False, "error": str(e)[:300]}


@app.get("/symbol-master/sync")
@app.post("/symbol-master/sync")
def symbol_master_sync(wait: bool = False):
    """
    Bug L fix: trigger the nightly symbol_master sync (Nifty 500 + sectoral
    index constituents → sector/industry/status). This module previously had
    no HTTP route, no scheduler call, and no GitHub Actions workflow — it
    only ever ran if someone SSHed in and executed the script by hand, so
    the symbol_master table was never created in production and the
    sector-relative-strength feature (§4/§5) silently did nothing.
    Runs in the background and returns immediately by default (poll
    /symbol-master/sync/status) since the full sync makes ~14 rate-limited
    NSE calls; pass wait=true to block and get the result inline instead.
    """
    try:
        from symbol_master_sync import run_sync, start_sync_background
        if wait:
            return run_sync()
        return start_sync_background()
    except Exception as e:
        logger.exception("symbol_master_sync failed")
        return {"ok": False, "error": str(e)[:300]}


@app.get("/symbol-master/sync/status")
def symbol_master_sync_status():
    """Poll the state of the current/last background symbol_master sync job."""
    try:
        from symbol_master_sync import get_sync_job
        return get_sync_job()
    except Exception as e:
        logger.exception("symbol_master_sync_status failed")
        return {"ok": False, "error": str(e)[:300]}


# ── Overnight orchestrator (2026-09-11) — see overnight_orchestrator.py's
# module docstring. Replaces the Render-era premarket/midnight GitHub
# Actions workflows now that this stack runs 24/7 on Oracle. ────────────────
@app.get("/overnight/status")
def overnight_status():
    from overnight_orchestrator import get_status
    return get_status()


@app.get("/overnight/config")
def overnight_get_config():
    from overnight_orchestrator import load_config
    return load_config()


@app.post("/overnight/config")
def overnight_set_config(
    enabled: Optional[bool] = None,
    datafeed_time: Optional[str] = None,
    premarket_time: Optional[str] = None,
    rest_between_steps_sec: Optional[int] = None,
):
    """Settings-page toggle + schedule editor. All fields optional — only
    the ones passed are changed."""
    from overnight_orchestrator import save_config
    patch = {}
    if enabled is not None:
        patch["enabled"] = enabled
    if datafeed_time is not None:
        patch["datafeed_time"] = datafeed_time
    if premarket_time is not None:
        patch["premarket_time"] = premarket_time
    if rest_between_steps_sec is not None:
        patch["rest_between_steps_sec"] = rest_between_steps_sec
    return {"ok": True, "config": save_config(patch)}


@app.post("/overnight/run")
def overnight_run(phase: str = "datafeed"):
    """Manual 'run now' button — bypasses the enabled toggle and the
    scheduled time entirely, same phases the nightly loop runs."""
    from overnight_orchestrator import start_phase_background
    if phase not in ("datafeed", "premarket"):
        return {"ok": False, "error": "phase must be 'datafeed' or 'premarket'"}
    return start_phase_background(phase)


@app.on_event("startup")
async def start_loop():
    global _task

    async def loop():
        await asyncio.sleep(20)
        while True:
            try:
                r = await asyncio.get_event_loop().run_in_executor(None, _select_1)
                if r.get("ok"):
                    logger.info("scheduler neon keepalive OK")
                else:
                    logger.debug("scheduler neon keepalive: %s", r.get("error"))
            except Exception as e:
                logger.debug("scheduler loop: %s", e)
            await asyncio.sleep(max(60, _NEON_INTERVAL))

    try:
        _task = asyncio.create_task(loop())
        logger.info("scheduler neon keep-alive started (%ss)", _NEON_INTERVAL)
    except Exception as e:
        logger.warning("scheduler loop start failed: %s", e)

    try:
        from overnight_orchestrator import scheduling_loop
        asyncio.create_task(scheduling_loop())
        logger.info("scheduler: overnight orchestrator loop started")
    except Exception as e:
        logger.warning("overnight orchestrator loop start failed: %s", e)
