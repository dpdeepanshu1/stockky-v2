"""Ops status routes: event-loop lag, Oracle latency, circuit-breaker states (read-only, no login; none is in admin_guard.PROTECTED). Moved out of main.py in E49, code unchanged (rules: gateway_routers/__init__.py)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

lag_router = APIRouter()


@lag_router.get("/ops/loop-lag")
async def ops_loop_lag():
    """Event-loop health: current lag and the worst stall since boot, with the stack that caused the last one."""
    try:
        import loop_watchdog
        return loop_watchdog.snapshot()
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "error": str(e)[:160]}


@lag_router.get("/ops/db-latency")
async def ops_db_latency():
    """Oracle latency: per-engine connect / execute count, avg, p50, p95, max, the last slow statement, pool state, keep-warm counters."""
    try:
        import oracle_compat
        return await asyncio.to_thread(oracle_compat.latency_snapshot)
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "error": str(e)[:160]}


circuit_router = APIRouter()


@circuit_router.get("/ops/circuit-status")
async def ops_circuit_status():
    """Inspect current circuit breaker states."""
    try:
        from circuit_breaker import all_snapshots
        return {"ok": True, "breakers": all_snapshots()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
