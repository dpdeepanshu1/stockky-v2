"""screening/vix_gate.py - India VIX (market fear) for the scalper's entry block and position size-down (group 330, 2026-10-12).

WHY: India VIX was not used in any decision. A high or spiking VIX widens gaps and shakes scalp stops out faster, whatever the candidate's
own score says. Two uses, both fail open:
  * block_reason(): no NEW entry while VIX >= config.VIX_BLOCK_ABOVE (0 = off) - called from trade_gates.market_gate_reject, so the cycle
    skips exactly like a weak-Nifty cycle.
  * size_mult():    position size x config.VIX_CAUTION_SIZE_MULT while VIX >= VIX_CAUTION_ABOVE or up >= VIX_SPIKE_PCT % on the day. Reads the
    cache ONLY (no network) so the sync entry path never waits; the cache is refreshed by the async market gate every cycle.

SOURCE: market-data GET /quote/INDIAVIX, cached VIX_CACHE_TTL_S (a failure is cached as "unknown" for FAILURE_RETRY_S). A reading outside 3..120
is not a VIX and counts as unknown. A cached reading older than 3 x the TTL is ignored by the sync readers (stale data never sizes a trade).
FAIL OPEN: unknown / error / timeout -> no opinion. Off with VIX_GATE_ENABLED=0.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional
from urllib.parse import quote

import config

logger = logging.getLogger("position-stocks-vix-gate")

FAILURE_RETRY_S = 60.0
_state: dict = {"ts": -1e9, "snap": None}


def reset() -> None:
    _state.update({"ts": -1e9, "snap": None})


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_row(body) -> Optional[dict]:
    """{"level", "change_pct"} from a /quote row, or None when it is not a usable VIX reading. Never raises."""
    try:
        if not isinstance(body, dict):
            return None
        level = _num(body.get("price"))
        if level is None:
            level = _num(body.get("cmp"))
        if level is None or not (3.0 <= level <= 120.0):
            return None
        chg = _num(body.get("day_change_pct"))
        if chg is None:
            prev = _num(body.get("previous_close"))
            if prev and prev > 0:
                chg = (level - prev) / prev * 100.0
        if chg is not None and abs(chg) > 80.0:
            chg = None
        return {"level": level, "change_pct": chg}
    except Exception:  # noqa: BLE001
        return None


async def _fetch() -> Optional[dict]:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{config.MARKET_DATA_URL}/quote/{quote('INDIAVIX', safe='&')}",
                             timeout=float(config.VIX_FETCH_TIMEOUT_S))
    if r.status_code != 200:
        raise RuntimeError(f"quote HTTP {r.status_code}")
    return parse_row(r.json())


async def refresh() -> Optional[dict]:
    """Cached VIX reading, re-fetched when older than the TTL. Never raises; None = unknown."""
    try:
        if not config.VIX_GATE_ENABLED:
            return None
        now = time.monotonic()
        age = now - _state["ts"]
        if _state["snap"] is not None and age < float(config.VIX_CACHE_TTL_S):
            return _state["snap"]
        if _state["snap"] is None and age < FAILURE_RETRY_S:
            return None
        try:
            snap = await _fetch()
        except Exception as e:  # noqa: BLE001
            logger.info("vix_gate: fetch failed (%s: %s) - no opinion", type(e).__name__, e)
            snap = None
        _state.update({"ts": time.monotonic(), "snap": snap})
        return snap
    except Exception:  # noqa: BLE001
        return None


def cached() -> Optional[dict]:
    """The cached reading without any network call; None when unknown or older than 3 x the TTL. Never raises."""
    try:
        if not config.VIX_GATE_ENABLED:
            return None
        snap = _state["snap"]
        if snap is None or time.monotonic() - _state["ts"] > 3.0 * float(config.VIX_CACHE_TTL_S):
            return None
        return snap
    except Exception:  # noqa: BLE001
        return None


def block_reason(snap: Optional[dict]) -> Optional[str]:
    try:
        if not config.VIX_GATE_ENABLED or not snap:
            return None
        limit = float(config.VIX_BLOCK_ABOVE or 0)
        if limit > 0 and snap["level"] >= limit:
            return f"VIX_HIGH:India VIX {snap['level']:.1f} >= {limit:.1f} - no new scalps while fear is this high"
        return None
    except Exception:  # noqa: BLE001
        return None


def size_mult(snap: Optional[dict] = None) -> float:
    """1.0 normally; config.VIX_CAUTION_SIZE_MULT (clamped 0.1..1.0) when VIX is elevated or spiking. Uses the cache when no snap is given."""
    try:
        if snap is None:
            snap = cached()
        if not config.VIX_GATE_ENABLED or not snap:
            return 1.0
        mult = min(1.0, max(0.1, float(config.VIX_CAUTION_SIZE_MULT)))
        hi = float(config.VIX_CAUTION_ABOVE or 0)
        spike = float(config.VIX_SPIKE_PCT or 0)
        if hi > 0 and snap["level"] >= hi:
            return mult
        if spike > 0 and snap.get("change_pct") is not None and snap["change_pct"] >= spike:
            return mult
        return 1.0
    except Exception:  # noqa: BLE001
        return 1.0
