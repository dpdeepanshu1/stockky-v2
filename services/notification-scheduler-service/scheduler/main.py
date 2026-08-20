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
        url = (
            os.getenv("CACHE_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("TRAINING_DATABASE_URL")
        )
        if not url:
            return {"ok": False, "error": "no_database_url"}
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
def hydrate_weekend(hour_idx: int | None = None, full: bool = False):
    """
    Time-sliced weekend hydration (force=true) for fundamentals + technical + events.
    hour_idx 0..47 selects the slice; omit to use current UTC hour % 48.
    full=true processes the entire universe (manual / GHA full pass — slow by design).
    """
    try:
        from weekend_hydrator import hydrate_batch
        return hydrate_batch(hour_idx=hour_idx, full=full)
    except Exception as e:
        logger.exception("hydrate_weekend failed")
        return {"ok": False, "error": str(e)[:300]}


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
