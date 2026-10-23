"""Buy Sniper edge report / evaluate and the Surprise static-cache peek. Moved out of main.py in E53, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

logger = logging.getLogger("api-gateway")

buys_edge_router = APIRouter()


@buys_edge_router.get("/api/scan/find-buys/edge")
@buys_edge_router.get("/scan/find-buys/edge")
async def find_buys_edge(days: int = 60, recent: int = 40, min_n: int = 20):
    """group361/362: does the Buy Sniper's daily-structure gate help? Per-verdict outcome stats (passed vs rejected), the gate edge, an advisory
    threshold calibration and the newest rows. Read-only; the kv read runs off the event loop."""
    try:
        import sniper_observation as _so
        return await asyncio.to_thread(_so.report, None, days, recent, min_n)
    except Exception as e:  # noqa: BLE001
        logger.warning("find-buys edge report failed: %s", e)
        return {"source": "sniper", "error": str(e)[:200]}


@buys_edge_router.post("/api/scan/find-buys/edge/evaluate")
@buys_edge_router.post("/scan/find-buys/edge/evaluate")
async def find_buys_edge_evaluate():
    """group361: run one outcome-evaluator pass now (fills T+1/5/10/20 returns on open rows). The same pass the QStash tick runs."""
    try:
        import sniper_observation as _so
        return {"ok": True, **(await asyncio.to_thread(_so.evaluate_pending))}
    except Exception as e:  # noqa: BLE001
        logger.warning("find-buys edge evaluate failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


surprise_static_router = APIRouter()


@surprise_static_router.get("/api/surprise/static")
@surprise_static_router.get("/surprise/static")
async def api_surprise_static(limit: int = 50):
    """Proxy / health peek of baselines (also tries local SQL if configured)."""
    try:
        from surprise_scanner import surprise_engine
        n = await asyncio.to_thread(surprise_engine.load_static_cache)  # group356: blocking SQL, off the event loop
        rows = list(surprise_engine.static_cache.values())[: max(1, min(limit, 200))]
        # JSON-safe
        out = []
        for r in rows:
            d = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in r.items()}
            out.append(d)
        return {"ok": True, "count": n, "rows": out, "source": "gateway_cache"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}
