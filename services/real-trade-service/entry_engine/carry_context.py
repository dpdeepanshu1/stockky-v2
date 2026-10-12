"""entry_engine/carry_context.py - market / fear / news context for the hold-or-sell decision at the close (group 330, 2026-10-12).

WHY: _select_overnight_holds already asks "is this position profitable, not extended, results not tomorrow". It never asked what the
MARKET is doing at 15:00: a stock can be green while Nifty is selling off into the close and VIX is jumping, and the next open then
gaps down on that context. This adds three context checks, evaluated per position, all fail open:
  1. MARKET : Nifty down config.CARRY_MIN_NIFTY_CHANGE_PCT or more vs the previous close (api-gateway /market/indices, fresh payloads only)
  2. FEAR   : India VIX high or spiking (entry_engine/vix_gate.carry_reason)
  3. NEWS   : negative news / events on the stock (entry_engine/event_risk.news_reason; CARRY_BLOCK_NEGATIVE_NEWS)
NOT covered (no verified live source at 15:00 IST): overseas / GIFT-Nifty cues. The overnight US-sector snapshot is last night's session, so
it says nothing about tonight's US open and is deliberately not used here.

Every check is "no opinion" on missing data. Blocking calls: wrap with asyncio.to_thread. Off with CARRY_CONTEXT_GATE_ENABLED=false.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import config

logger = logging.getLogger("real-trade-carry-context")

_CACHE_TTL_S = 120.0
FAILURE_RETRY_S = 60.0
_lock = threading.Lock()
_nifty: dict = {"ts": -1e9, "pct": None}


def reset() -> None:
    with _lock:
        _nifty.update({"ts": -1e9, "pct": None})


def parse_indices(body) -> Optional[float]:
    """Nifty % change vs the previous close from a /market/indices body, or None (absent, unusable, stale or zero-fallback payload)."""
    try:
        if not isinstance(body, dict) or body.get("stale") or body.get("fallback"):
            return None
        v = float((body.get("nifty_vs_prev_close") or {}).get("change_pct"))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, AttributeError):
        return None


def _fetch_nifty() -> Optional[float]:
    import httpx
    with httpx.Client(timeout=float(config.CARRY_INDICES_TIMEOUT_S)) as client:
        r = client.get(f"{config.API_GATEWAY_URL}/market/indices", params={"force_refresh": "false"})
    if r.status_code != 200:
        raise RuntimeError(f"indices HTTP {r.status_code}")
    return parse_indices(r.json())


def nifty_change_pct() -> Optional[float]:
    """Cached Nifty % change vs previous close, or None when unknown. Blocks on a cache miss. Never raises."""
    try:
        now = time.monotonic()
        with _lock:
            age = now - _nifty["ts"]
            if age < (_CACHE_TTL_S if _nifty["pct"] is not None else FAILURE_RETRY_S):
                return _nifty["pct"]
        try:
            pct = _fetch_nifty()
        except Exception as e:  # noqa: BLE001
            logger.info("carry_context: indices fetch failed (%s: %s) - no opinion", type(e).__name__, e)
            pct = None
        with _lock:
            _nifty.update({"ts": time.monotonic(), "pct": pct})
        return pct
    except Exception:  # noqa: BLE001
        return None


def market_reason(nifty_pct: Optional[float]) -> Optional[str]:
    try:
        if nifty_pct is None:
            return None
        floor = float(config.CARRY_MIN_NIFTY_CHANGE_PCT)
        if floor < 0 and nifty_pct <= floor:
            return f"CARRY_MARKET_WEAK: Nifty {nifty_pct:+.2f}% vs previous close <= {floor:+.2f}% - market selling into the close"
        return None
    except Exception:  # noqa: BLE001
        return None


def reason(symbol: str, nifty_pct: Optional[float] = None, vix_snap: Optional[dict] = None, *, check_news: bool = True) -> Optional[str]:
    """First carry-context objection for this symbol, or None. Pass nifty_pct / vix_snap fetched once for the whole batch. Never raises."""
    try:
        if not config.CARRY_CONTEXT_GATE_ENABLED:
            return None
        r = market_reason(nifty_pct)
        if r:
            return r
        from entry_engine import event_risk, vix_gate
        r = vix_gate.carry_reason(vix_snap)
        if r:
            return r
        if check_news and config.CARRY_BLOCK_NEGATIVE_NEWS:
            return event_risk.news_reason(symbol)
        return None
    except Exception:  # noqa: BLE001
        return None
