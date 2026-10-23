"""Six small routes moved out of main.py in E55: /circuits, /ops/rate-limits, /metrics, /ops/qstash/publish, /ready and the data-feed audit-missing report.
Bodies are unchanged except that every main.py name is read as `ref.<name>` at call time (see gateway_support/__init__.py).

`build_bound_routes(ref)` returns a namespace: one router per route (include each where the route used to be) and the handler functions by name."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse


def build_bound_routes(ref):
    circuits_router = APIRouter()
    rate_limits_router = APIRouter()
    metrics_router = APIRouter()
    qstash_publish_router = APIRouter()
    ready_router = APIRouter()
    audit_missing_router = APIRouter()

    @circuits_router.get("/circuits")
    def circuits_status():
        """Circuit breaker states for downstream dependencies."""
        snaps = ref.all_snapshots()
        open_n = sum(1 for v in snaps.values() if v.get("state") == "open")
        ref.metrics.set_gauge("stockky_circuits_open", float(open_n))
        return {"circuits": snaps}

    @rate_limits_router.get("/ops/rate-limits")
    @rate_limits_router.get("/api/rate-limits")
    @rate_limits_router.get("/api/ops/rate-limits")
    async def ops_rate_limits():
        """Dashboard payload: rate-limit events + circuit breakers (last 1h)."""
        # snapshot() does blocking I/O: a Neon/Oracle kv_get for the durable aggregate and, when Redis is
        # enabled, an lrange. Called inline from this async handler it stalled the whole event loop for a
        # DB round trip on every dashboard poll (same bug class as record(), which already hands its I/O to a
        # pool), so every other request waited behind it. all_snapshots() is in-memory and cheap, so it
        # stays here; only the monitor read goes to a worker thread.
        circuits = ref.all_snapshots()
        return await asyncio.to_thread(ref.rate_limit_monitor.snapshot, circuits=circuits)

    @metrics_router.get("/metrics")
    def metrics_endpoint(request: Request):
        """JSON metrics by default; ?format=prom for Prometheus text."""
        fmt = (request.query_params.get("format") or "json").lower()
        if fmt in ("prom", "prometheus", "text"):
            return Response(content=ref.metrics.prometheus_text(), media_type="text/plain; version=0.0.4")
        return ref.metrics.snapshot()

    @qstash_publish_router.post("/ops/qstash/publish")
    async def ops_qstash_publish(request: Request):
        """Manually publish a delayed callback (admin / scheduler)."""
        if ref.qstash_client is None or not ref.qstash_client.enabled():
            return {"ok": False, "error": "QStash not configured (set QSTASH_TOKEN)"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        dest = body.get("url") or body.get("destination")
        try:
            delay = int(body.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "delay_seconds must be an integer"}, status_code=400)
        if not dest:
            return ref.qstash_client.schedule_gateway_tick(delay_seconds=delay, body=body)
        return ref.qstash_client.publish(dest, body.get("payload") or {}, delay_seconds=delay)

    @ready_router.get("/ready")
    def ready():
        is_ready = ref._durable_cache_configured()
        if not is_ready:
            return JSONResponse({"ready": False}, status_code=503)
        return {"ready": True}

    @audit_missing_router.get("/api/feed/audit-missing")
    @audit_missing_router.get("/data-feed/audit-missing")
    @audit_missing_router.get("/api/data-feed/audit-missing")
    async def audit_missing_feed_data(limit: int = 500, cache: bool = True):
        """
        Audits data-feed records to calculate the exact DB Health Score.
        MUST be registered BEFORE /api/feed/{symbol} so 'audit-missing' is not
        captured as a symbol path (silent 404 / null → UI 0% and dashes).

        Memoised for AUDIT_TTL_SEC (pass cache=false to force a recount) — this walks
        every tracked symbol checking five fields each, and the DB Health tab
        refetches it on every mount, which was most of that tab's load time.

        BUG FIX (2026-09-12): on a cache miss (first call after AUDIT_TTL_SEC
        expiry, cache=false, or a cold start) the full per-symbol walk below used
        to run synchronously inline in this async route — same class of bug as
        /surprise/ipo/list earlier this session, except this one can iterate up
        to `limit` symbols (callers pass up to 5000) each doing their own
        store/DB lookup, making it the single heaviest blocking call in this
        file. Now offloaded to a worker thread via run_in_threadpool so a slow
        cache-miss audit can't stall every other route on the shared event loop.
        """
        if cache:
            hit = ref._audit_cache_get(f"feed_audit:{limit}")
            if hit is not None:
                return hit
        return await ref.run_in_threadpool(ref._compute_feed_audit, limit)

    return SimpleNamespace(
        circuits_router=circuits_router, rate_limits_router=rate_limits_router, metrics_router=metrics_router,
        qstash_publish_router=qstash_publish_router, ready_router=ready_router, audit_missing_router=audit_missing_router,
        circuits_status=circuits_status, ops_rate_limits=ops_rate_limits, metrics_endpoint=metrics_endpoint,
        ops_qstash_publish=ops_qstash_publish, ready=ready, audit_missing_feed_data=audit_missing_feed_data,
    )
