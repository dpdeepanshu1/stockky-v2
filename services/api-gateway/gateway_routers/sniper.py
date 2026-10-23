"""Buy Sniper find-buys route and its two helpers (structure enrichment, observation hand-off). Moved out of main.py in E54, code unchanged.
main.py owns `_cb_get` (circuit breaker), `_structure_metric` and TECHNICAL_URL; they reach this module through gateway_support (bound by main before this
module is imported), via call-time trampolines, so a test that patches `main._cb_get` still takes effect."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request

import gateway_support
import ms_passthrough

logger = logging.getLogger("api-gateway")
TECHNICAL_URL = gateway_support.technical_url


async def _cb_get(*args, **kwargs):
    return await gateway_support.cb_get(*args, **kwargs)


def _structure_metric(*args, **kwargs) -> None:
    return gateway_support.structure_metric(*args, **kwargs)


sniper_router = APIRouter()


async def _sniper_attach_structure(payload: Dict[str, Any]) -> int:
    """group357 (WS-4): the UI posts trimmed rows with no structure summary, so fetch GET {TECHNICAL_URL}/structure/{sym} for the best-conviction
    candidates that have none (at most SNIPER_STRUCTURE_FETCH_LIMIT, 4 at a time, inside a SNIPER_STRUCTURE_FETCH_TIMEOUT_S budget) and attach the
    flat summary in place. Fail-open: any error / bad schema / timeout leaves the row without a summary = no opinion = card unchanged.
    Returns how many rows got a summary. Honors STRUCTURE_PASSTHROUGH=0 and SNIPER_STRUCTURE_MODE=off (nothing fetched)."""
    try:
        if not ms_passthrough.enabled():
            return 0
        import buy_sniper as _bs
        try:
            limit = max(0, int(((os.getenv("SNIPER_STRUCTURE_FETCH_LIMIT") or "").strip() or "12")))
        except ValueError:
            limit = 12
        try:
            budget = max(1.0, float(((os.getenv("SNIPER_STRUCTURE_FETCH_TIMEOUT_S") or "").strip() or "10")))
        except ValueError:
            budget = 10.0
        syms = _bs.symbols_needing_structure(payload, limit)
        if not syms:
            return 0
        sums: Dict[str, Dict[str, Any]] = {}
        sem = asyncio.Semaphore(4)

        async def _one(client, sym: str) -> None:
            async with sem:
                try:
                    resp = await _cb_get(client, "technical", f"{TECHNICAL_URL}/structure/{sym}", timeout=5.0)
                    if resp.status_code != 200:
                        _structure_metric("stockky_sniper_structure_fetch_total", 1, outcome="http_error")
                        return
                    data = resp.json()
                    if ms_passthrough.safe_block(data) is None:
                        _structure_metric("stockky_sniper_structure_fetch_total", 1, outcome="no_block")
                        return
                    sums[sym] = ms_passthrough.safe_summary(data)
                    _structure_metric("stockky_sniper_structure_fetch_total", 1, outcome="ok")
                except Exception:  # noqa: BLE001
                    _structure_metric("stockky_sniper_structure_fetch_total", 1, outcome="error")

        async with httpx.AsyncClient() as client:
            tasks = [asyncio.ensure_future(_one(client, sym)) for sym in syms]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=budget)
            except asyncio.TimeoutError:
                for t in tasks:
                    t.cancel()
                _structure_metric("stockky_sniper_structure_fetch_total", 1, outcome="timeout")
        n = 0
        for row in _bs._payload_rows(payload):
            if isinstance(row, dict) and not row.get("structure_summary") and _bs._sg_symbol(row) in sums:
                row["structure_summary"] = sums[_bs._sg_symbol(row)]
                n += 1
        return n
    except Exception as e:  # noqa: BLE001
        logger.debug("sniper structure attach failed (no opinion): %s", e)
        return 0


def _sniper_observe(payload: Dict[str, Any], out: Dict[str, Any]) -> None:
    """group361: hand one find-buys answer to the observation recorder on a worker thread. Fire-and-forget: the response never waits for the
    kv store and any failure (module missing, no running loop, executor shut down) is swallowed. SNIPER_OBS_ENABLED=0 stores nothing."""
    try:
        import sniper_observation as _so
        if not _so.enabled():
            return
        asyncio.get_running_loop().run_in_executor(None, _so.record, payload, out)
    except Exception as e:  # noqa: BLE001
        logger.debug("sniper observation hand-off failed (ignored): %s", e)


@sniper_router.post("/api/scan/find-buys")
@sniper_router.post("/scan/find-buys")
async def find_actionable_buys(request: Request):
    """
    Buy Sniper — return 1–4 high-conviction setups from a scan result list.
    Always HTTP 200: empty suggestions is a valid outcome (not 400).
    Body: { "stocks": [...], "target_count": 4, "min_conviction": 58 }
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"stocks": payload if isinstance(payload, list) else []}
    try:
        from buy_sniper import suggestions_from_scan_payload
        await _sniper_attach_structure(payload)   # group357: fail-open; adds ms_* summaries so the structure gate can judge the rows
        out = suggestions_from_scan_payload(payload)
        if not isinstance(out, dict):
            return {"ok": True, "count": 0, "suggestions": []}
        out.setdefault("ok", True)
        # An explicit None used to survive setdefault (suggestions: null, count: 0).
        if not isinstance(out.get("suggestions"), list):
            out["suggestions"] = []
        out.setdefault("count", len(out["suggestions"]))
        _sniper_observe(payload, out)   # group361: store what the structure gate saw (worker thread, fail-open, never changes the answer)
        return out
    except Exception as e:
        logger.exception("find-buys failed")
        return {
            "ok": False,
            "count": 0,
            "suggestions": [],
            "error": str(e)[:300],
            "message": "Sniper could not evaluate candidates",
        }
