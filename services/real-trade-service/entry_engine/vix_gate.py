"""entry_engine/vix_gate.py - India VIX (market fear) checks for entries, sizing and overnight carry (group 330, 2026-10-12).

WHY: India VIX was not used in any decision. A high or spiking VIX means wider gaps and faster stop-outs for every long, whatever the
stock's own score says. Three uses, all fail open:
  * entry_block_reason : no NEW BUY while VIX >= config.VIX_BLOCK_ABOVE (0 = off)
  * size_mult          : position risk x config.VIX_CAUTION_SIZE_MULT while VIX >= VIX_CAUTION_ABOVE or up >= VIX_SPIKE_PCT % on the day
  * carry_reason       : no overnight carry while VIX >= VIX_CARRY_BLOCK_ABOVE or up >= VIX_CARRY_SPIKE_PCT % on the day (0 = off)

SOURCE: market-data GET /quote/INDIAVIX (price = the VIX level; day_change_pct or previous_close = the move on the day), cached for
config.VIX_CACHE_TTL_S. A failed fetch is cached as "unknown" for FAILURE_RETRY_S, so a dead market-data does not add a timeout to every
candidate. A reading outside 3..120 is not a VIX (wrong symbol, stale zero) and counts as unknown.

FAIL OPEN, ALWAYS: unknown reading, bad body, timeout, any exception -> no opinion. snapshot() blocks on a cache miss: call it through
asyncio.to_thread from async code. Off with VIX_GATE_ENABLED=false.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import config

logger = logging.getLogger("real-trade-vix-gate")

FAILURE_RETRY_S = 60.0
_lock = threading.Lock()
_state: dict = {"ts": -1e9, "snap": None}


def reset() -> None:
    with _lock:
        _state.update({"ts": -1e9, "snap": None})


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_row(body) -> Optional[dict]:
    """{"level": float, "change_pct": float | None} from a /quote row, or None when it is not a usable VIX reading. Never raises."""
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
            chg = None                       # not a believable one-day VIX move: the previous close is wrong
        return {"level": level, "change_pct": chg}
    except Exception:  # noqa: BLE001
        return None


def _fetch() -> Optional[dict]:
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    with httpx.Client(timeout=float(config.VIX_FETCH_TIMEOUT_S)) as client:
        r = client.get(f"{MARKET_DATA_URL}/quote/INDIAVIX")
    if r.status_code != 200:
        raise RuntimeError(f"quote HTTP {r.status_code}")
    return parse_row(r.json())


def snapshot() -> Optional[dict]:
    """The cached VIX reading ({"level", "change_pct"}) or None when unknown. Blocks on a cache miss. Never raises."""
    try:
        if not config.VIX_GATE_ENABLED:
            return None
        now = time.monotonic()
        with _lock:
            age = now - _state["ts"]
            if _state["snap"] is not None and age < float(config.VIX_CACHE_TTL_S):
                return _state["snap"]
            if _state["snap"] is None and age < FAILURE_RETRY_S:
                return None
        try:
            snap = _fetch()
        except Exception as e:  # noqa: BLE001
            logger.info("vix_gate: fetch failed (%s: %s) - no opinion", type(e).__name__, e)
            snap = None
        with _lock:
            _state.update({"ts": time.monotonic(), "snap": snap})
        return snap
    except Exception:  # noqa: BLE001
        return None


def entry_block_reason(snap: Optional[dict]) -> Optional[str]:
    try:
        if not config.VIX_GATE_ENABLED or not snap:
            return None
        limit = float(config.VIX_BLOCK_ABOVE or 0)
        if limit > 0 and snap["level"] >= limit:
            return f"VIX_HIGH: India VIX {snap['level']:.1f} >= {limit:.1f} - no new entries while fear is this high"
        return None
    except Exception:  # noqa: BLE001
        return None


def size_mult(snap: Optional[dict]) -> float:
    """1.0 normally; config.VIX_CAUTION_SIZE_MULT (clamped to 0.1..1.0) when VIX is elevated or spiking. Never raises."""
    try:
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


def carry_reason(snap: Optional[dict]) -> Optional[str]:
    try:
        if not config.VIX_GATE_ENABLED or not snap:
            return None
        lim = float(config.VIX_CARRY_BLOCK_ABOVE or 0)
        if lim > 0 and snap["level"] >= lim:
            return f"VIX_HIGH_CARRY: India VIX {snap['level']:.1f} >= {lim:.1f} - not carried overnight"
        spike = float(config.VIX_CARRY_SPIKE_PCT or 0)
        if spike > 0 and snap.get("change_pct") is not None and snap["change_pct"] >= spike:
            return f"VIX_SPIKE_CARRY: India VIX +{snap['change_pct']:.1f}% on the day - not carried overnight"
        return None
    except Exception:  # noqa: BLE001
        return None
