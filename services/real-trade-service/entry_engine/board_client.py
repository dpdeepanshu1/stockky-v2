"""entry_engine/board_client.py - group332: read the Market Board (position-stocks-service GET /board) for the shortlist nudge.

Cached ENTRY_BOARD_CACHE_TTL_S, short timeout, never raises: any failure = {} = "no nudge" (the board is a ranking input, not a gate)."""
from __future__ import annotations

import logging
import time
from typing import Dict

import httpx

import config

logger = logging.getLogger("real-trade-board-client")

_cache: dict = {"at": float("-inf"), "rows": {}}


def index_rows(payload) -> Dict[str, dict]:
    """symbol -> board row (bullish and bearish; a bullish row wins when a symbol is in both)."""
    out: Dict[str, dict] = {}
    if not isinstance(payload, dict):
        return out
    for key, side in (("bearish", "bear"), ("bullish", "bull")):
        for r in payload.get(key) or []:
            if isinstance(r, dict) and r.get("symbol"):
                out[str(r["symbol"])] = dict(r, side=r.get("side") or side)
    return out


async def get_rows() -> Dict[str, dict]:
    if not getattr(config, "ENTRY_BOARD_NUDGE_ENABLED", True):
        return {}
    now = time.monotonic()
    if now - _cache["at"] < float(config.ENTRY_BOARD_CACHE_TTL_S):
        return _cache["rows"]
    rows: Dict[str, dict] = {}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(config.ENTRY_BOARD_URL, params={"limit": 60}, timeout=float(config.ENTRY_BOARD_TIMEOUT_S))
        if r.status_code == 200:
            body = r.json()
            if not body.get("stale"):            # yesterday's closing board must not rank today's entries
                rows = index_rows(body)
    except Exception as e:  # noqa: BLE001
        logger.debug("board client: fetch failed (no nudge): %s", e)
    _cache.update(at=now, rows=rows)             # failures cached too, so a dead service is retried every TTL
    return rows


def reset() -> None:
    _cache.update(at=float("-inf"), rows={})
